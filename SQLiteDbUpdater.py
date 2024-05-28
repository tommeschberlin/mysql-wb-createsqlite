import os
import re
import sqlite3
import logging

if not 'ExportSQLiteError' in dir():
    ExportSQLiteError = ImportError

class SQLiteDbUpdater:
    # create update using path for database to update/create and sql script for creating
    def __init__(self, dbPath, createDbSql ) -> None:
        self.dbPath = dbPath
        self.createDbSql = createDbSql
        self.logger = None
        self.dbFileName = os.path.basename(self.dbPath)
        self.dbName = os.path.splitext(self.dbFileName)[0]
        self.dbTmpFileName = self.dbFileName + "~"
        self.dbRestoreFileName = self.dbName + "_restore.sql"
        self.dbDefinitionFileName =  self.dbName + "_definition.sql"
        self.confirmRequestCallback = None
        self.workDir = os.path.dirname( dbPath )
        self.logFile = os.path.join( self.workDir, self.dbName + ".log" )
        self.dbTableInfo = {}

    def log(self, msg, level=logging.INFO):
        if self.logger:
            logging.log( level, msg )

    def enableLogging(self):
        self.logger = logging.getLogger("SQLiteDbUpdater")
        logging.basicConfig(filename=self.logFile, filemode='wt', level=logging.DEBUG, format='%(asctime)s %(levelname)s %(message)s', datefmt='%H:%M:%S')

    def getTableInfo(cursor, tableName):
        tableInfoByColName = {}
        tableInfoByColIdx = {}
        cursor.execute( "PRAGMA table_info(\"%s\");" % tableName )
        info = cursor.fetchall()
        for idx,col in enumerate(info):
            info = { 'cid': col[0], 'name': col[1], 'type': col[2], 'notnull': col[3], 'dflt_value': col[4], 'pk': col[5] }
            tableInfoByColIdx[idx] = info
            tableInfoByColName[col[1]] = info

        return tableInfoByColIdx, tableInfoByColName
            
    # create database info to decide later howto dump/restore data
    def getDbTableInfo(dbFileName):
        dbTableInfo = {}
        conn = sqlite3.connect(dbFileName)
        try:
            cur = conn.cursor()
            cur.execute( "select name from sqlite_master where type='table'" )
            tableNames = cur.fetchall()
            for (tableName,) in tableNames:
                cur.execute( "select * from \"%s\"" % tableName )
                rows = cur.fetchall()
                infoByColIdx, infoByColName = SQLiteDbUpdater.getTableInfo(cur, tableName)
                dbTableInfo[tableName] = { 'byIdx': infoByColIdx, 'byName': infoByColName, 'containsData' : len(rows) > 0 }
        finally:
            conn.close()
        
        return dbTableInfo

    # check if database already contains data
    def containsData(dbTableInfo):
        for tableName, tableInfo in dbTableInfo.items():
            if tableInfo['containsData']:
                return True
        return False

    def restoreTableByRow(tableRows, newTableName, file):
        for row in tableRows:
            sqlLine = 'INSERT INTO "%s" VALUES%s;' % (newTableName, row)
            file.write('%s\n' % sqlLine)

    def restoreTableByRowCol(tableRows, oldTableInfo, colNamesToRestore, newTableName, file):
        for row in tableRows:
            sqlColumnNames = []
            sqlColumnValues = []
            for colName in colNamesToRestore:
                colInfo = oldTableInfo['byName'][colName]
                idx = colInfo['']
                sqlColumnNames.append( colName )
                if isinstance(row[idx], str):
                    sqlColumnValues.append( "\'" + row[idx] + "\'" )
                else:
                    sqlColumnValues.append( str(row[idx]) )
            
            sqlLine = 'INSERT INTO "%s"(%s) VALUES(%s)' % (newTableName, ','.join(sqlColumnNames), ','.join(sqlColumnValues) )
            file.write('%s\n' % sqlLine)

    # dump data of already existing database
    def dumpData(dbFileName, dbDumpFileName, dumpStrategy):
        conn = sqlite3.connect(dbFileName)
        try:
            cur = conn.cursor()
            with open(dbDumpFileName, 'w') as f:
                cur.execute( "select name from sqlite_master where type='table'" )
                tableNames = cur.fetchall()
                for (tableName,) in tableNames:
                    strategy = dumpStrategy.get(tableName)
                    if strategy:
                        cur.execute( "select * from %s" % tableName )
                        rows = cur.fetchall()
                        strategy(rows, f)
        finally:                    
            conn.close()

    # restore dumped data to temporary created database
    def restoreData( dbFileName, dbDumpFileName ):
        with open(dbDumpFileName, 'rt') as f:
            sql = f.read()
            conn = sqlite3.connect(dbFileName)
            cur = conn.cursor()
            try:
                cur.executescript(sql)
                conn.commit()
            finally:
                cur.close()
                conn.close()

    # replace the dbname with the choosen filename stem                
    def substituteDbNameInSql(self, sql):
        pattern = r"ATTACH \"([^ \"]+)\" AS \"([^ \";]+)\""
        match = re.search(pattern, sql)
        if not match:
            raise ExportSQLiteError( 'Error', 'Cant evaluate/replace ATTACH ... line!')
        prevDbName = match.group(2)
        sql = re.sub(pattern, "ATTACH \"%s\" AS \"%s\"" %(self.dbTmpFileName,self.dbName), sql)
        sql = re.sub( "\"" +  prevDbName + "\"\\.", "\"" + self.dbName + "\".", sql)
        return sql

    def commentIndexInSql(self, sql):
        pattern = r"\n(CREATE INDEX[^\n]*)"
        match = re.search(pattern, sql)
        if not match:
            return
        sql = re.sub(pattern, "\n-- %s" % match.group(1), sql)
        return sql
    
    # stores sql creation script for inspection purposes, create backup of an already existing one
    def storeSql(sql, sqlFileName):
        sqlTmpFileName = sqlFileName + "~"

        if os.path.isfile(sqlTmpFileName):
            os.remove( sqlTmpFileName )

        if os.path.isfile(sqlFileName):
            os.rename( sqlFileName, sqlTmpFileName )

        with open(sqlFileName, 'w') as f:
            f.write(sql)

    def findTableByFingerprint(tableInfo, newDbTableInfo):
        for newTableName, newTableInfo in newDbTableInfo.items():
            if newTableInfo == tableInfo:
                return newTableName
        return None
    
    def evaluateRestoreStrategy(self, oldDbTableInfo, newDbTableInfo):
        restoreStrategy = {}
        for tableName, oldTableInfo in oldDbTableInfo.items():
            if not oldDbTableInfo[tableName]['containsData']:
                continue
            newTableInfo = newDbTableInfo.get(tableName)
            newTableName = tableName
            if not newDbTableInfo:
                # check for renamed table
                newTableName = SQLiteDbUpdater.findTableByFingerprint(oldTableInfo, newDbTableInfo)
                if not newTableName:
                    info = "Table '%s' not found in new DB-schema, also not by column-fingerprint!" % tableName
                    info += "If table was renamed and also has changed colums try to rename it in the first run and change columns an a second run!"
                    self.log( info )
                    continue
                self.log( "Table '%s' was probably renamed, will try to restore data to table '%s!" % (tableName, newTableName) )
                newTableInfo = newDbTableInfo.get(newTableName)

            strategy = ""
            # no columndef changed
            if oldTableInfo['byIdx'] == newTableInfo['byIdx']:
                restoreStrategy[tableName] = lambda tableRows, file, nameOfNewTable=newTableName : \
                    SQLiteDbUpdater.restoreTableByRow(tableRows, nameOfNewTable, file )
                strategy = "ByRow"
            else:
                self.log( "Table '%s' fingerprint has been changed, maybe data will be not restored correctly!" % tableName, logging.WARN )
                # retrieving change info
                addedCols = []
                addedNotNullCols = []
                changedTypeCols = []
                changedToNotNullCols = []
                removedCols = []
                colNamesToRestore = []
                for name,colInfo in oldTableInfo['byName'].items():
                    if not name in newTableInfo['byName']:
                        removedCols.append( name )
                    else:
                        colNamesToRestore.append[name]
                        if newTableInfo['byName'][name]['type'] != colInfo['type']:
                            changedTypeCols.append(name)
                        elif newTableInfo['byName'][name]['notnull'] != colInfo['notnull'] and \
                             newTableInfo['byName'][name]['notnull'] == 1:
                            changedToNotNullCols.append(name)

                for name,colInfo in newTableInfo['byName'].items():
                    if not name in oldTableInfo['byName']:
                        addedCols.append( name )
                        if colInfo['notnull'] == 1:
                            addedNotNullCols.append(name)

                if len(changedToNotNullCols) or len(addedNotNullCols):
                    self.log( "Column(s) '%s' has been created/changed to have 'notNull' values, if restoring of data leads to problems, "
                              "start without 'notNull' in the first run, fill in data and then change definition to 'notNull' in the second run!"
                               % ','.join( addedNotNullCols + changedToNotNullCols ), logging.WARN )

                if len(changedTypeCols):
                    self.log( "Type of column(s) '%s' has been changed, if restoring of data leads to problems, "
                              "adapt data before change the datatype!" % ','.join( changedTypeCols ), logging.WARN )
                    
                # only col footprint changed, only added, only removed or only moved cols
                if (len(addedCols) * len(removedCols)) == 0:
                    restoreStrategy[tableName] = lambda tableRows, file, nameOfNewTable=newTableName : \
                        SQLiteDbUpdater.restoreTableByRowCol( tableRows, oldTableInfo, colNamesToRestore, nameOfNewTable, file )
                    strategy = "ByRowCol"
                # check for renamed/ cols
                elif len(addedCols) == len(removedCols):
                    self.log( "Column(s) '%s' has been added and column(s) '%s' has been removed, this will be interpreted as changed col names!"
                              "If this is leads to problems, try to reorder, rename, remove or add only one column in a single run!"
                              % (','.join( addedCols ), ','.join( removedCols )), logging.WARN )
                    restoreStrategy[tableName] = lambda tableRows, file, nameOfNewTable=newTableName : \
                        SQLiteDbUpdater.restoreTableByRow(tableRows, nameOfNewTable, file )
                    strategy = "ByRow"
                else:
                    self.log( "Column(s) '%s' has been added this matches not the number of column(s) '%s' which has been removed!"
                              "Restoring is not possible, try to reorder, rename, remove or add only one column in a single run!"
                              % (','.join( addedCols ), ','.join( removedCols )), logging.ERROR )
                    raise ExportSQLiteError( 'Error', 'Restoring is not possible for table: %s!' % tableName)

            self.log( "Dump/Restore table \"%s\" by strategy: %s" % ( tableName, strategy ))

        return restoreStrategy

    # udpdate/create database in a most secure way
    # all updates changes will be made in a temporary created db
    # if all stuff went well, replace the current db with the temporary created one
    def update(self):
        self.log('Update started')
        os.chdir( self.workDir )

        # set choosen filename stem as db name in sql definition
        sql = self.substituteDbNameInSql( self.createDbSql )
        sql = self.commentIndexInSql( sql )
        SQLiteDbUpdater.storeSql( sql, self.dbDefinitionFileName)

        # create db in dbTmpFileName
        if os.path.isfile(self.dbTmpFileName):
            os.remove( self.dbTmpFileName )
        conn = sqlite3.connect(self.dbTmpFileName)
        try:        
            cur = conn.cursor()
            cur.executescript(sql)
            conn.commit()
        finally:
            cur.close()
            conn.close()

        # backup/restore data
        if os.path.isfile(self.dbFileName):
            oldDbTableInfo = SQLiteDbUpdater.getDbTableInfo( self.dbFileName )
            if SQLiteDbUpdater.containsData(oldDbTableInfo):
                newDbTableInfo = SQLiteDbUpdater.getDbTableInfo( self.dbTmpFileName )
                restoreStrategy = self.evaluateRestoreStrategy(oldDbTableInfo, newDbTableInfo)
                SQLiteDbUpdater.dumpData(self.dbFileName, self.dbRestoreFileName, restoreStrategy)
                SQLiteDbUpdater.restoreData(self.dbTmpFileName, self.dbRestoreFileName)

        # on success replace dbFileName by dbTmpFileName
        if os.path.isfile(self.dbFileName):
            os.remove( self.dbFileName )
        os.rename( self.dbTmpFileName, self.dbFileName  )

        self.log('Update finished')
