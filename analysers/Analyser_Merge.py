#!/usr/bin/env python
#-*- coding: utf-8 -*-

###########################################################################
##                                                                       ##
## Copyrights Frédéric Rodrigo 2012                                      ##
##                                                                       ##
## This program is free software: you can redistribute it and/or modify  ##
## it under the terms of the GNU General Public License as published by  ##
## the Free Software Foundation, either version 3 of the License, or     ##
## (at your option) any later version.                                   ##
##                                                                       ##
## This program is distributed in the hope that it will be useful,       ##
## but WITHOUT ANY WARRANTY; without even the implied warranty of        ##
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the         ##
## GNU General Public License for more details.                          ##
##                                                                       ##
## You should have received a copy of the GNU General Public License     ##
## along with this program.  If not, see <http://www.gnu.org/licenses/>. ##
##                                                                       ##
###########################################################################

import io
import bz2
import csv
import hashlib
import inspect
import psycopg2.extras
import psycopg2.extensions
import os
import os.path
import time
import zipfile
import tempfile
import json
from collections import defaultdict
from Analyser_Osmosis import Analyser_Osmosis
from modules import downloader
from modules import PointInPolygon

sql_schema = """
DO language 'plpgsql' $$
BEGIN
  IF NOT EXISTS (SELECT * FROM information_schema.schemata WHERE schema_name = '%(schema)s' ) THEN
    CREATE SCHEMA %(schema)s;
  END IF;
END $$
"""

sql00 = """
DROP TABLE IF EXISTS %(official)s CASCADE;
CREATE TABLE %(schema)s.%(official)s (
    ref varchar(65534),
    tags hstore,
    tags1 hstore,
    fields hstore,
    geom geography
)
"""

sql01_ref = """
SELECT
    %(x)s AS _x,
    %(y)s AS _y,
    *
FROM
    %(table)s
WHERE
    %(where)s
"""

sql01_geo = """
SELECT
    %(x)s AS _x,
    %(y)s AS _y,
    *
FROM
    %(table)s
WHERE
    %(x)s IS NOT NULL AND
    %(y)s IS NOT NULL AND
    %(x)s::varchar != '' AND
    %(y)s::varchar != '' AND
    %(where)s
"""

sql02 = """
INSERT INTO
    %(official)s
VALUES (
    %(ref)s,
    %(tags)s,
    %(tags1)s,
    %(fields)s,
    ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography
)
"""

sql03 = """
CREATE INDEX index_ref_%(official)s ON %(official)s(ref);
CREATE INDEX index_geom_%(official)s ON %(official)s USING GIST(geom);
"""

sql10 = """
CREATE TABLE missing_official AS
SELECT
    official.ref,
    ST_AsText(official.geom),
    official.tags,
    official.fields,
    official.geom
FROM
    %(official)s AS official
    LEFT JOIN osm_item ON
        %(joinClause)s
WHERE
    osm_item.id IS NULL
"""

sql11 = """
CREATE INDEX missing_official_index_ref ON missing_official(ref);
CREATE INDEX missing_official_index_geom ON missing_official USING GIST(geom);
"""

sql12 = """
SELECT * FROM missing_official;
"""

sql20 = """
CREATE TABLE missing_osm AS
SELECT
    osm_item.id,
    osm_item.type,
    CASE
        WHEN osm_item.geom IS NOT NULL THEN ST_AsText(osm_item.geom)
        ELSE ST_AsText(any_locate(osm_item.type, osm_item.id))
    END,
    osm_item.tags,
    osm_item.geom,
    osm_item.shape
FROM
    osm_item
    LEFT JOIN %(official)s AS official ON
        %(joinClause)s
WHERE
    osm_item.ref IS NULL AND
    official.ref IS NULL
"""

sql21 = """
CREATE INDEX missing_osm_index_shape ON missing_osm USING GIST(shape)
"""

sql22 = """
SELECT * FROM missing_osm
"""

sql23 = """
SELECT
    osm_item.id,
    osm_item.type,
    CASE
        WHEN osm_item.geom IS NOT NULL THEN ST_AsText(osm_item.geom)
        ELSE ST_AsText(any_locate(osm_item.type, osm_item.id))
    END,
    osm_item.tags,
    osm_item.geom
FROM
    osm_item
    LEFT JOIN %(official)s AS official ON
        %(joinClause)s
WHERE
    osm_item.ref IS NOT NULL AND
    official.ref IS NULL
"""

sql30 = """
SELECT
    DISTINCT ON (id)
    missing_osm.id,
    missing_osm.type,
    CASE
        WHEN missing_osm.geom IS NOT NULL THEN ST_AsText(missing_osm.geom)
        ELSE ST_AsText(any_locate(missing_osm.type, missing_osm.id))
    END,
    missing_official.tags AS official_tags,
    missing_official.fields AS official_fields,
    missing_osm.tags AS osm_tags
FROM
    missing_official
    JOIN missing_osm ON
        %(joinClause)s
ORDER BY
    missing_osm.id
    %(orderBy)s
"""

sql40 = """
CREATE TABLE match AS
SELECT
    osm_item.id,
    osm_item.type,
    osm_item.tags,
    osm_item.geom
FROM
    osm_item
    JOIN %(official)s AS official ON
        %(joinClause)s
"""

sql41 = """
(
    SELECT
        id::bigint AS osm_id,
        type::varchar AS osm_type,
        tags::hstore,
        ST_X(geom::geometry)::float AS lon,
        ST_Y(geom::geometry)::float AS lat
    FROM
        match
) UNION (
    SELECT
        NULL::bigint AS osm_id,
        NULL::varchar AS osm_type,
        tags::hstore,
        ST_X(geom::geometry)::float AS lon,
        ST_Y(geom::geometry)::float AS lat
    FROM
        missing_official
) UNION (
    SELECT
        id::bigint AS osm_id,
        type::varchar AS osm_type,
        tags::hstore,
        ST_X(geom::geometry)::float AS lon,
        ST_Y(geom::geometry)::float AS lat
    FROM
        missing_osm
)
"""

sql50 = """
SELECT
    osm_item.id,
    ST_AsText(osm_item.geom),
    ST_AsText(official.geom)
FROM
    %(official)s AS official
    JOIN osm_item ON
        %(joinClause)s AND
        NOT official.geom && osm_item.geom
"""

sql60 = """
SELECT
    osm_item.id,
    osm_item.type,
    ST_AsText(osm_item.geom),
    official.tags,
    osm_item.tags,
    official.fields AS official_fields
FROM
    %(official)s AS official
    JOIN osm_item ON
        %(joinClause)s
WHERE
    official.tags1 - osm_item.tags - 'source'::text != ''::hstore
"""

class Source:
    def __init__(self, attribution = None, millesime = None, url = None, name = None, encoding = "utf-8", file = None, fileUrl = None, fileUrlCache = 30, zip = None, filter = None):
        """
        Describe the source file.
        @param encoding: file charset encoding
        @param file: file name in storage
        @param urlFile: remote URL of source file
        @param fileUrlCache: days for file in cache
        @param zip: extract file from zip
        @param filter: lambda expression applied on text file before loading
        """
        self.attribution = attribution
        self.millesime = millesime
        self.encoding = encoding
        self.file = file
        self.fileUrl = fileUrl
        self.fileUrlCache = fileUrlCache
        self.zip = zip
        self.filter = filter

        if self.millesime and "%s" in self.millesime:
            self.millesime_re = re.compile(self.millesime.replace("%s", ".*"))

    def time(self):
        if self.file:
            return int(os.path.getmtime("merge_data/"+self.file)+.5)
        elif self.fileUrl:
            if self.zip:
                f = downloader.urlopen(self.fileUrl, self.fileUrlCache)
                date_time = zipfile.ZipFile(f, 'r').getinfo(self.zip).date_time
                return time.mktime(date_time + (0, 0, -1))
            else:
                return int(downloader.urlmtime(self.fileUrl, self.fileUrlCache)+.5)

    def path(self):
        if self.file:
            return "merge_data/"+self.file
        elif self.fileUrl:
            # Do nothing about ZIP
            return downloader.path(self.fileUrl, self.fileUrlCache)

    def open(self):
        if self.file:
            f = bz2.BZ2File("merge_data/"+self.file)
        elif self.fileUrl:
            f = downloader.urlopen(self.fileUrl, self.fileUrlCache)
            if self.zip:
                f = zipfile.ZipFile(f, 'r').open(self.zip)
        if self.encoding not in ("UTF8", "UTF-8"):
            f = io.StringIO(f.read().decode(self.encoding, 'ignore'))
            f.seek(0)
        if self.filter:
            f = io.StringIO(self.filter(f.read()))
            f.seek(0)
        return f

    def as_tag_value(self):
        if "%s" in self.attribution:
            return self.attribution % self.millesime
        else:
            return " - ".join(filter(lambda x: x!= None, [self.attribution, self.millesime]))

    def match_attribution(self, s):
        if "%s" not in self.attribution:
            return self.attribution in s
        else:
            self.attribution_re.match(s)

class Parser:
    def header(self):
        pass

    def import_(self, table, srid, osmosis):
        pass

    def close(self):
        pass

class CSV(Parser):
    def __init__(self, source, separator = ',', null = '', header = True, quote = '"', csv = True):
        """
        Describe the CSV file format, mainly for postgres COPY command in order to load data, but also for other thing, like load header.
        Setting param as None disable parameter into the COPY command.
        @param source: source file reader
        @param separator: one char separator
        @param null: string loaded à NULL
        @param header: CSV have header row
        @param quote: one char string delimiter
        @param csv: load file as CSV on COPY command
        """
        self.source = source
        self.separator = separator
        self.null = null
        self.have_header = header
        self.quote = quote
        self.csv = csv

        self.f = None

    def header(self):
        self.f = self.source.open()
        if self.have_header:
            header = self.f.readline().strip().strip(self.separator)
            csvf = io.BytesIO(header.encode('utf-8'))
            self.f.seek(0)
            return csv.reader(csvf, delimiter=self.separator, quotechar=self.quote).next()

    def import_(self, table, srid, osmosis):
        self.f = self.f or self.source.open()
        copy = "COPY %s FROM STDIN WITH %s %s %s %s %s" % (
            table,
            ("DELIMITER AS '%s'" % self.separator) if self.separator != None else "",
            ("NULL AS '%s'" % self.null) if self.null != None else "",
            "CSV" if self.csv else "",
            "HEADER" if self.csv and self.header else "",
            ("QUOTE '%s'" % self.quote) if self.csv and self.quote else "")
        osmosis.giscurs.copy_expert(copy, self.f)

    def close(self):
        self.f.close()

class JSON(Parser):
    def __init__(self, source, extractor = lambda json: json):
        """
        Load JSON file data.
        @param source: source file reader
        @param extractor: lamba returning an interable
        """
        self.source = source
        self.extractor = extractor

        self.json = None

    def header(self):
        self.json = self.extractor(json.loads(self.source.open().read()))
        return self.json[0].keys()

    def import_(self, table, srid, osmosis):
        self.json = self.json or self.extractor(json.loads(self.source.open().read))
        insert_statement = u"insert into %s (%%s) values %%s" % table
        for row in self.json:
            columns = row.keys()
            values = map(lambda column: unicode(row[column]) if row[column] != None else None, columns)
            osmosis.giscurs.execute(insert_statement, (psycopg2.extensions.AsIs(u",".join(map(lambda c: "\"%s\"" % c, columns))), tuple(values)))

class SHP(Parser):
    def __init__(self, source):
        """
        Load Shape file data.
        @param source: source file reader
        """
        self.source = source

    def header(self):
        return True

    def import_(self, table, srid, osmosis):
        tmp_file = tempfile.NamedTemporaryFile(delete = False)
        tmp_file.close()
        unzip = "unzip -o -d %s_ %s" % (tmp_file.name, self.source.path())
        if os.system(unzip):
            raise Exception("unzip error")
        shp2pgsql = "shp2pgsql -e -k -W \"%s\" -s \"%s\" \"%s_/%s\" \"%s\" > \"%s\"" % (
            self.source.encoding,
            srid,
            tmp_file.name,
            self.source.zip,
            table,
            tmp_file.name
        )
        if os.system(shp2pgsql):
            raise Exception("shp2pgsql error")
        sql = open(tmp_file.name, 'r').read().split(";\n")
        for s in sql:
            if s != "":
                osmosis.giscurs.execute(s)
        os.remove(tmp_file.name)

class Load(object):
    def __init__(self, x = ("NULL",), y = ("NULL",), srid = 4326, create = None,
            select = {}, where = lambda res: True, xFunction = lambda i: i, yFunction = lambda i: i):
        """
        Describ the conversion of data set loaded with COPY into the database into an other table more usable for processing.
        @param x: the name of x column, as or converted to longitude, can be a SQL expression formatted as ("SQL CODE",)
        @param y: the name of y column, as or converted to latitude, can be a SQL expression formatted as ("SQL CODE",)
        @param srid: the projection of x and y coordinate
        @param create: the data base table description, generated by default from file header et format
        @param select: dict reformatted as SQL to filter row import before conversion, prefer this as the where param
        @param where: lambda expression taking row as dict and returning boolean to determine whether or not inserting the row into the table
        @param xFunction: lambda expression for convert x content column before reprojection, identity by default
        @param yFunction: lambda expression for convert y content column before reprojection, identity by default
        """
        self.x = x
        self.y = y
        self.srid = srid
        self.create = create
        self.select = select
        self.where = where
        self.xFunction = xFunction
        self.yFunction = yFunction

    def formatCSVSelect(self):
        where = []
        for k, v in self.select.items():
            if isinstance(v, list):
                cond = "\"%s\" IN ('%s')" % (k, "','".join(map(lambda i: i.replace("'", "''"), filter(lambda i: i != None, v))))
                if None in v:
                    cond = "(" + cond + " OR \"%s\" IS NULL)" % k
                where.append(cond)
            elif v == None or v == False:
                where.append("\"%s\" IS NULL" % k)
            elif v == True:
                where.append("\"%s\" IS NOT NULL" % k)
            elif '%' in v:
                where.append("\"%s\" LIKE '%s'" % (k, v.replace("'", "''")))
            else:
                where.append("\"%s\" = '%s'" % (k, v.replace("'", "''")))
        if where == []:
            return "1=1"
        else:
            return " AND ".join(where)

    def run(self, osmosis, parser, mapping, db_schema, table_base_name, time):
        """
        @return if data loaded in data base
        """
        table_base_name = table_base_name[15:] # Remove start "analyser_merge_"
        if len(table_base_name) <= 63: # 63 max postgres relation name
            table = table_base_name
        else:
            table = table_base_name[-(63-10):]+hashlib.md5(table_base_name).hexdigest()[-10:]

        self.data = False
        def setDataTrue():
            self.data=True
        osmosis.run0("SELECT * FROM meta WHERE name='%s' AND update>=%s" % (table, time), lambda res: setDataTrue())
        if not self.data:
            osmosis.logger.log(u"Load source into database")
            osmosis.run("DROP TABLE IF EXISTS %s" % table)
            if not self.create:
                header = parser.header()
                if header:
                    if header != True:
                        self.create = ",".join(map(lambda c: "\"%s\" VARCHAR(65534)" % c, header))
                else:
                    raise AssertionError("No table schema provided")
            osmosis.run(sql_schema % {"schema": db_schema})
            if self.create:
                osmosis.run("CREATE TABLE %s.%s (%s)" % (db_schema, table, self.create))
            parser.import_(table, self.srid, osmosis)
            osmosis.run("DELETE FROM meta WHERE name = '%s'" % table)
            osmosis.run("INSERT INTO meta VALUES ('%s', %s, NULL)" % (table, time))
            osmosis.run0("COMMIT")
            osmosis.run0("BEGIN")
            parser.close()

        # Convert
        if len(table_base_name) <= 63-4-11: # 63 max postgres relation name, 11 is index name prefix
            tableOfficial = table_base_name+"_off"
        else:
            tableOfficial = table_base_name[-(63-4-11-10):]+"_off"+hashlib.md5(table_base_name).hexdigest()[-10:]

        self.data = False
        def setData(res):
            self.data=res
        osmosis.run0("SELECT bbox FROM meta WHERE name='%s' AND bbox IS NOT NULL AND update IS NOT NULL AND update>=%s" % (tableOfficial, time), lambda res: setData(res))
        if not self.data:
            self.pip = PointInPolygon.PointInPolygon(self.polygon_id) if self.polygon_id else None
            osmosis.logger.log(u"Convert data to tags")
            osmosis.run(sql_schema % {"schema": db_schema})
            osmosis.run(sql00 % {"schema": db_schema, "official": tableOfficial})
            giscurs = osmosis.gisconn.cursor(cursor_factory=psycopg2.extras.DictCursor)
            giscurs_getpoint = osmosis.gisconn.cursor(cursor_factory=psycopg2.extras.DictCursor)
            def insertOfficial(res):
                x = self.xFunction(res[0])
                y = self.yFunction(res[1])
                if x and y and self.where(res):
                    giscurs_getpoint.execute("SELECT ST_AsText(ST_Transform(ST_SetSRID(ST_MakePoint(%(x)s, %(y)s), %(SRID)s), 4326))" % {"x": x, "y": y, "SRID": self.srid})
                    lonLat = self.osmosis.get_points(giscurs_getpoint.fetchone()[0])[0]
                    lonLat = [float(lonLat["lon"]), float(lonLat["lat"])]
                    if not self.pip or self.pip.point_inside_polygon(lonLat[0], lonLat[1]):
                        for k in res.iterkeys():
                            if res[k] != None and isinstance(res[k], basestring):
                                res[k] = ' '.join(res[k].split()) # Strip and remove duplicate space
                        tags = mapping.generate.tagFactory(res)
                        tags[1].update(tags[0])
                        giscurs.execute(sql02.replace("%(official)s", tableOfficial), {
                            "ref": tags[1].get(mapping.osmRef) if mapping.osmRef != "NULL" else None,
                            "tags": tags[1],
                            "tags1": tags[0],
                            "fields": dict(zip(dict(res).keys(), map(lambda x: unicode(x), dict(res).values()))),
                            "lon": lonLat[0], "lat": lonLat[1]
                        })
            if isinstance(self.x, tuple):
                self.x = self.x[0]
            else:
                self.x = "\"%s\"" % self.x
            if isinstance(self.y, tuple):
                self.y = self.y[0]
            else:
                self.y = "\"%s\"" % self.y
            osmosis.run0((sql01_ref if mapping.osmRef != "NULL" else sql01_geo) % {"table":table, "x":self.x, "y":self.y, "where":self.formatCSVSelect()}, insertOfficial)
            if self.srid:
                giscurs.execute("SELECT ST_AsText(ST_Envelope(ST_Extent(geom::geometry))::geography) FROM %s" % tableOfficial)
                self.bbox = giscurs.fetchone()[0]
            else:
                self.bbox = None
            osmosis.run(sql03 % {"official": tableOfficial})

            giscurs_getpoint.close()
            giscurs.close()

            osmosis.run("DELETE FROM meta WHERE name='%s'" % tableOfficial)
            if self.bbox != None:
                osmosis.run("INSERT INTO meta VALUES ('%s', %s, '%s')" % (tableOfficial, time, self.bbox))
            osmosis.run0("COMMIT")
            osmosis.run0("BEGIN")
        else:
            self.bbox = self.data[0]

        if not(self.srid and not self.bbox): # Abort condition
            return tableOfficial

class Select:
    def __init__(self, types = [], tags = {}):
        """
        On witch OSM we try to join data set.
        @param types: object types, array of "relations", "ways" and "nodes"
        @param tags: dict of tags or array of dicts, array mean "OR"
        """
        self.types = types
        self.tags = tags

class Generate:
    def __init__(self, missing_official_fix = True, static1 = {}, static2 = {}, mapping1 = {}, mapping2 = {}, text = lambda tags, fields: {}):
        """
        How result error file is build.
        @param missing_official_fix: boolean to generate or not new object with quickfix
        @param static1: dict of primary tags apply as is
        @param static2: dict of secondary tags apply as is, not checked on update process
        @param mapping1: dict of primary tags, if value is string then data set column value is take, else lambda
        @param mapping2: dict of secondary tags, if value is string then data set column value is take, else lambda, not checked on update process
        @param text: lambda return string, describe this error
        """
        self.missing_official_fix = missing_official_fix
        self.static1 = static1
        self.static2 = static2
        self.mapping1 = mapping1
        self.mapping2 = mapping2
        self.text = text

    def eval_staticGroup(self, static, analyser):
        for tag, colomn in static.items():
            if inspect.isfunction(colomn) or inspect.ismethod(colomn):
                r = colomn(analyser)
                if r:
                    static[tag] = unicode(r)

    def eval_static(self, analyser):
        self.eval_staticGroup(self.static1, analyser)
        self.eval_staticGroup(self.static2, analyser)

    def tagFactoryGroup(self, res, static, mapping):
        tags = dict(static)
        for tag, colomn in mapping.items():
            if inspect.isfunction(colomn) or inspect.ismethod(colomn):
                r = colomn(res)
                if r:
                    tags[tag] = unicode(r)
            elif colomn and res[colomn]:
                tags[tag] = unicode(res[colomn])

        return tags

    def tagFactory(self, res):
        tags = self.tagFactoryGroup(res, self.static1, self.mapping1)
        tags_secondary = self.tagFactoryGroup(res, self.static2, self.mapping2)
        return [tags, tags_secondary]

class Mapping:
    def __init__(self, select = Select(), osmRef = "NULL", conflationDistance = None, extraJoin = None, generate = Generate()):
        """
        How data is mapped with OSM data.
        @param select: fetch OSM data, see Select
        @param osmRef: the osm key for join data on reference
        @param conflationDistance: if no osmRef, do do conflation, use this threshold
        @param extraJoin: additional key condition to join on
        @param generate: build the result, see Generate
        """
        self.select = select
        self.osmRef = osmRef
        self.conflationDistance = conflationDistance
        self.extraJoin = extraJoin
        self.generate = generate

class Analyser_Merge(Analyser_Osmosis):

    def __init__(self, config, logger, url, name, parser, load = Load(), mapping = Mapping()):
        """
        @param url: remote URL of data source, webpage
        @param name: official name of the data set
        """
        Analyser_Osmosis.__init__(self, config, logger)
        self.url = url
        self.name = name
        self.parser = parser
        self.load = load
        self.mapping = mapping

        if hasattr(self, 'missing_official'):
            self.classs[self.missing_official["class"]] = self.missing_official
        else:
            self.missing_official = None
        if hasattr(self, 'missing_osm'):
            self.classs[self.missing_osm["class"]] = self.missing_osm
        else:
            self.missing_osm = None
        if hasattr(self, 'possible_merge'):
            self.classs[self.possible_merge["class"]] = self.possible_merge
        else:
            self.possible_merge = None
        if hasattr(self, 'moved_official'):
            self.classs[self.moved_official["class"]] = self.moved_official
        else:
            self.moved_official = None
        if hasattr(self, 'update_official'):
            self.classs[self.update_official["class"]] = self.update_official
        else:
            self.update_official = None

        if not isinstance(self.mapping.select.tags, list):
            self.mapping.select.tags = [self.mapping.select.tags]
        self.mapping.generate.eval_static(self)
        self.load.osmosis = self
        self.load.polygon_id = self.config.polygon_id

    def float_comma(self, val):
        return float(val.replace(',', '.'))

    def degree(self, val):
        if u'°' in val:
            # 01°13'23,8 -> 1,334388
            return reduce(lambda sum, i: sum * 60 + i, map(lambda i: float(i.replace(u',', u'.')), filter(lambda i: i != '', val.replace(u'°', u"'").split(u"'"))), 0) / 3600
        else:
            return val

    def source(self, a):
        return a.parser.source.as_tag_value()

    def lastUpdate(self):
        time = [self.parser.source.time()]
        h = inspect.getmro(self.__class__)
        h = h[:-3]
        for c in h:
            time.append(int(os.path.getmtime(inspect.getfile(c))+.5))
        return max(time)

    def analyser_osmosis(self):
        table = self.load.run(self, self.parser, self.mapping, self.config.db_user, self.__class__.__name__.lower(), self.lastUpdate())
        if not table:
            self.logger.log(u"Empty bbox, abort")
            return

        # Extract OSM objects
        if self.load.srid:
          typeSelect = {'N': 'geom', 'W': 'linestring', 'R': 'relation_locate(id)'}
          typeGeom = {'N': 'geom', 'W': 'way_locate(linestring)', 'R': 'relation_locate(id)'}
          if self.mapping.osmRef == "NULL" or self.possible_merge:
            typeShape = {'N': 'geom', 'W': 'ST_Envelope(linestring)', 'R': 'relation_shape(id)'}
          else:
            typeShape = {'N': 'NULL', 'W': 'NULL', 'R': 'NULL'}
        else:
          typeSelect = {'N': 'NULL', 'W': 'NULL', 'R': 'NULL'}
          typeGeom = {'N': 'NULL', 'W': 'NULL', 'R': 'NULL'}
          typeShape = {'N': 'NULL', 'W': 'NULL', 'R': 'NULL'}
        self.logger.log(u"Retrive OSM item")
        where = "(" + (") OR (".join(map(lambda x: self.where(x), self.mapping.select.tags))) + ")"
        self.run("CREATE TABLE osm_item AS " +
            ("UNION".join(
                map(lambda type:
                    ("""(
                    SELECT
                        '%(type)s'::char(1) AS type,
                        id,
                        CASE
                            WHEN (tags->'%(ref)s') IS NULL THEN NULL
                            ELSE trim(both from regexp_split_to_table(tags->'%(ref)s', ';'))
                        END AS ref,
                        %(geom)s::geography AS geom,
                        %(shape)s::geography AS shape,
                        tags
                    FROM
                        %(from)s
                    WHERE""" + ("""
                        %(geomSelect)s IS NOT NULL AND""" if self.load.srid else "") + ("""
                        ST_SetSRID(ST_GeomFromText('%(bbox)s'), 4326) && %(geomSelect)s AND""" if self.load.bbox and self.load.srid else "") + """
                        tags != ''::hstore AND
                        %(where)s)""") % {"type":type[0].upper(), "ref":self.mapping.osmRef, "geomSelect":typeSelect[type[0].upper()], "geom":typeGeom[type[0].upper()], "shape":typeShape[type[0].upper()], "from":type, "bbox":self.load.bbox, "where":where},
                    self.mapping.select.types
                )
            ))
        )
        if self.mapping.osmRef != "NULL":
            self.run("CREATE INDEX osm_item_index_ref ON osm_item(ref)")
        self.run("CREATE INDEX osm_item_index_shape ON osm_item USING GIST(shape)")

        joinClause = []
        if self.mapping.osmRef != "NULL":
            joinClause.append("official.ref = osm_item.ref")
        elif self.load.srid:
            joinClause.append("ST_DWithin(official.geom, osm_item.shape, %s)" % self.mapping.conflationDistance)
        if self.mapping.extraJoin:
            joinClause.append("official.tags->'%(tag)s' = osm_item.tags->'%(tag)s'" % {"tag": self.mapping.extraJoin})
        joinClause = " AND\n".join(joinClause) + "\n"

        # Missing official
        self.run(sql10 % {"official": table, "joinClause": joinClause})
        self.run(sql11)
        if self.missing_official:
            self.run(sql12, lambda res: {
                "class": self.missing_official["class"],
                "subclass": str(self.stablehash("%s%s"%(res[0],res[1]))),
                "self": lambda r: [0]+r[1:],
                "data": [self.node_new, self.positionAsText],
                "text": self.mapping.generate.text(defaultdict(lambda:None,res[2]), defaultdict(lambda:None,res[3])),
                "fix": {"+": res[2]} if self.mapping.generate.missing_official_fix and res[2] != {} else None,
            } )

        if self.mapping.osmRef != "NULL":
            self.run(sql20 % {"official": table, "joinClause": joinClause})
            self.run(sql21)
            if self.missing_osm:
                # Missing OSM
                self.run(sql22, lambda res: {
                    "class": self.missing_osm["class"],
                    "data": [self.typeMapping[res[1]], None, self.positionAsText]
                } )
                # Invalid OSM
                self.run(sql23 % {"official": table, "joinClause": joinClause}, lambda res: {
                    "class": self.missing_osm["class"],
                    "data": [self.typeMapping[res[1]], None, self.positionAsText]
                } )

            # Possible merge
            if self.possible_merge:
                possible_merge_joinClause = []
                possible_merge_orderBy = ""
                if self.load.srid:
                    possible_merge_joinClause.append("ST_DWithin(missing_official.geom, missing_osm.shape, %s)" % self.mapping.conflationDistance)
                    possible_merge_orderBy = ", ST_Distance(missing_official.geom, missing_osm.shape) ASC"
                if self.mapping.extraJoin:
                    possible_merge_joinClause.append("missing_official.tags->'%(tag)s' = missing_osm.tags->'%(tag)s'" % {"tag": self.mapping.extraJoin})
                possible_merge_joinClause = " AND\n".join(possible_merge_joinClause) + "\n"
                self.run(sql30 % {"joinClause": possible_merge_joinClause, "orderBy": possible_merge_orderBy}, lambda res: {
                    "class": self.possible_merge["class"],
                    "subclass": str(self.stablehash("%s%s"%(res[0],str(res[3])))),
                    "data": [self.typeMapping[res[1]], None, self.positionAsText],
                    "text": self.mapping.generate.text(defaultdict(lambda:None,res[3]), defaultdict(lambda:None,res[4])),
                    "fix": self.mergeTags(res[5], res[3], self.mapping.osmRef),
                } )

            self.dumpCSV("SELECT ST_X(geom::geometry) AS lon, ST_Y(geom::geometry) AS lat, tags FROM %s" % table, "", ["lon","lat"], lambda r, cc:
                list((r['lon'], r['lat'])) + cc
            )

            self.run(sql40 % {"official": table, "joinClause": joinClause})
            self.dumpCSV(sql41, ".byOSM", ["osm_id","osm_type","lon","lat"], lambda r, cc:
                list((r['osm_id'], r['osm_type'], r['lon'], r['lat'])) + cc
            )

            file = io.open("%s/%s.metainfo.csv" % (self.config.dst_dir, self.name), "w", encoding="utf8")
            file.write(u"file,origin,osm_date,official_non_merged,osm_non_merged,merged\n")
            if self.missing_official:
                self.giscurs.execute("SELECT COUNT(*) FROM missing_official;")
                official_non_merged = self.giscurs.fetchone()[0]
            else:
                official_non_merged = 0
            self.giscurs.execute("SELECT COUNT(*) FROM missing_osm;")
            osm_non_merged = self.giscurs.fetchone()[0]
            self.giscurs.execute("SELECT COUNT(*) FROM match;")
            merged = self.giscurs.fetchone()[0]
            file.write(u"\"%s\",\"%s\",FIXME,%s,%s,%s\n" % (self.name, self.parser.source.fileUrl or self.url, official_non_merged, osm_non_merged, merged))
            file.close()

        # Moved official
        if self.moved_official:
            self.run(sql50 % {"official": table, "joinClause": joinClause}, lambda res: {
                "class": self.moved_official["class"],
                "data": [self.node_full, self.positionAsText],
            } )

        # Update official
        if self.update_official:
            self.run(sql60 % {"official": table, "joinClause": joinClause}, lambda res: {
                "class": self.update_official["class"],
                "subclass": str(self.stablehash("%s%s"%(res[0],str(res[4])))),
                "data": [self.typeMapping[res[1]], None, self.positionAsText],
                "text": self.mapping.generate.text(defaultdict(lambda:None,res[3]), defaultdict(lambda:None,res[5])),
                "fix": self.mergeTags(res[4], res[3], self.mapping.osmRef),
            } )



    def mergeTags(self, osm, official, ref):
        fix = {"+": {}, "~":{}}
        for o in official:
            if o in osm:
                if osm[o] == official[o]:
                    pass
                else:
                    if o == "source":
                        if self.parser.source.attribution:
                            for s in osm[o].split(";"):
                                if self.parser.source.match_attribution(s):
                                    fix["~"][o] = osm[o].replace(s, self.parser.source.as_tag_value())
                                    break
                        else:
                            fix["~"][o] = osm[o]+";"+official[o]
                    else:
                        fix["~"][o] = official[o]
            else:
                fix["+"][o] = official[o]
        if osm.get(ref) and ";" in osm[ref]:
            del(fix["~"][ref]) # Do not replace multiple ref by only one
        keys = [s for s in (fix["+"].keys() + fix["~"].keys()) if s != "name" and not s.startswith("source")]
        if "name" in osm and "name" in official and osm["name"] != official["name"] and len(keys) != 0:
            fix0 = {"+": fix["+"], "~": dict(fix["~"])}
            del(fix0["~"]["name"])
            fix = [fix0, fix]
        return fix

    def dumpCSV(self, sql, ext, head, callback):
        self.giscurs.execute(sql)
        row = []
        column = {}
        while True:
            many = self.giscurs.fetchmany(1000)
            if not many:
                break
            for res in many:
                row.append(res)
                for k in res['tags'].keys():
                    if k not in column:
                        column[k] = 1
                    else:
                        column[k] += 1
        column = sorted(column, key=column.get, reverse=True)
        column = filter(lambda a: a!=self.mapping.osmRef and not a in self.mapping.select.tags[0], column)
        column = [self.mapping.osmRef] + self.mapping.select.tags[0].keys() + column
        file = bz2.BZ2File(u"%s/%s-%s%s.csv.bz2" % (self.config.dst_dir, self.name, self.__class__.__name__, ext), "w")
        file.write((u"%s\n" % ','.join(head + column)).encode("utf-8"))
        for r in row:
            cc = []
            for c in column:
                tags = r['tags']
                if c in tags:
                    cc.append(tags[c])
                else:
                    cc.append(None)
            cc = map(lambda x: (x if not ',' in x or not '"' else "\"%s\"" % x.replace('"','\\\"')).replace('\r','').replace('\n',''), map(lambda x: '' if not x else unicode(x), callback(r, cc)))
            file.write((u"%s\n" % ','.join(cc).rstrip(',')).encode("utf-8"))
        file.close()

    def where(self, tags):
        clauses = []
        for k, v in tags.items():
            clauses.append("tags?'%s'" % k)
            if isinstance(v, list):
                clauses.append("tags->'%s' IN ('%s')" % (k, "','".join(map(lambda i: i.replace("'", "''"), v))))
            elif v:
                clauses.append("tags->'%s' = '%s'" % (k, v.replace("'", "''")))
        return " AND ".join(clauses)
