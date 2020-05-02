# This file is part of Tautulli.
#
#  Tautulli is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  Tautulli is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with Tautulli.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import unicode_literals
from future.builtins import object

import arrow
import os
import sqlite3
import shutil
import threading
import time

import plexpy
if plexpy.PYTHON2:
    import helpers
    import logger
else:
    from plexpy import logger
    from plexpy.helpers import cast_to_int


FILENAME = "tautulli.db"
db_lock = threading.Lock()


def validate_database(database=None):
    try:
        connection = sqlite3.connect(database, timeout=20)
    except sqlite3.OperationalError as e:
        logger.error("Tautulli Database :: Invalid database specified: %s", e)
        return 'Invalid database specified'
    except ValueError as e:
        logger.error("Tautulli Database :: Invalid database specified: %s", e)
        return 'Invalid database specified'
    except Exception as e:
        logger.error("Tautulli Database :: Uncaught exception: %s", e)
        return 'Uncaught exception'

    return 'success'


def import_tautulli_db(database=None, method=None, backup=True):
    db_validate = validate_database(database=database)
    if not db_validate == 'success':
        logger.error("Tautulli Database :: Failed to import Tautulli database: %s", db_validate)
        return False

    if method not in ('merge', 'overwrite'):
        logger.error("Tautulli Database :: Failed to import Tautulli database: invalid import method '%s'", method)
        return False

    if backup:
        # Make a backup of the current database first
        logger.info("Tautulli Database :: Creating a database backup before importing.")
        if not make_backup():
            logger.error("Tautulli Database :: Failed to import Tautulli database: failed to create database backup")
            return False

    logger.info("Tautulli Database :: Importing Tautulli database '%s' with import method '%s'...", database, method)

    db = MonitorDatabase()
    db.connection.execute('BEGIN IMMEDIATE')
    db.connection.execute('ATTACH ? AS import_db', [database])

    # Get the current number of used ids in the session_history table
    session_history_seq = db.select_single('SELECT seq FROM sqlite_sequence WHERE name = "session_history"')

    if method == 'merge':
        # Create a temporary table so we can reindex session_history.reference_id after merging
        logger.info("Tautulli Database :: Creating temporary database table to re-index grouped session history.")
        db.action('CREATE TABLE IF NOT EXISTS temp (id INTEGER PRIMARY KEY, old_id, new_id)')
        db.action('INSERT INTO temp (old_id) SELECT id FROM import_db.session_history')
        db.action('UPDATE temp SET new_id = id + ?', [session_history_seq['seq']])

    # Keep track of all table columns so that duplicates can be removed after importing
    session_history_tables = ('session_history', 'session_history_metadata', 'session_history_media_info')
    session_history_columns = []
    table_columns = {}

    tables = db.select('SELECT name FROM import_db.sqlite_master '
                       'WHERE type = "table" AND name NOT LIKE "sqlite_%"')
    for table in tables:
        table_name = table['name']
        if table_name == 'sessions':
            # Skip temporary sessions table
            continue

        logger.info("Tautulli Database :: Importing database table '%s'.", table_name)

        if method == 'overwrite':
            # Clear the table and reset the autoincrement ids
            db.action('DELETE FROM {table}'.format(table=table_name))
            db.action('DELETE FROM sqlite_sequence WHERE name = ?', [table_name])

        # Get the list of columns to import
        columns = db.select('PRAGMA import_db.table_info({table})'.format(table=table_name))
        import_columns = [c['name'] for c in columns if not c['pk']]
        insert_columns = ', '.join(import_columns)

        table_columns[table_name] = insert_columns
        if table == 'session_history':
            session_history_columns = import_columns

        # Insert the data with ignore instead of replace to be safe
        db.action('INSERT OR IGNORE INTO {table} ({columns}) '
                  'SELECT {columns} FROM import_db.{table}'.format(table=table_name, columns=insert_columns))

    db.connection.execute('DETACH import_db')

    if method == 'merge':
        # Reindex session_history.reference_id
        logger.info("Tautulli Database :: Re-indexing grouped session history.")
        db.action('UPDATE session_history SET reference_id = (SELECT new_id FROM temp WHERE old_id = reference_id)'
                  'WHERE id > ?', [session_history_seq['seq']])

        if session_history_columns:
            # Remove reference_id column from the list of session_history table columns to get unique rows
            columns = ', '.join([c for c in session_history_columns if c != 'reference_id'])
            # Remove session_history ids to KEEP from the temp table
            db.action('DELETE FROM temp WHERE new_id IN ('
                      'SELECT MIN(id) FROM session_history '
                      'GROUP BY {columns})'.format(columns=columns))

        for table, columns in table_columns.items():
            logger.info("Tautulli Database :: Removing duplicate rows from database table '%s'.", table)
            if table in session_history_tables:
                db.action('DELETE FROM {table} WHERE id IN (SELECT new_id FROM temp)'.format(table=table))
            else:
                db.action('DELETE FROM {table} WHERE id NOT IN '
                          '(SELECT MIN(id) FROM {table} GROUP BY {columns})'.format(table=table, columns=columns))

        logger.info("Tautulli Database :: Deleting temporary database table.")
        db.action('DROP TABLE temp')

    db.action('VACUUM')

    logger.info("Tautulli Database :: Tautulli database import complete.")


def integrity_check():
    monitor_db = MonitorDatabase()
    result = monitor_db.select_single('PRAGMA integrity_check')
    return result


def clear_table(table=None):
    if table:
        monitor_db = MonitorDatabase()

        logger.debug("Tautulli Database :: Clearing database table '%s'." % table)
        try:
            monitor_db.action('DELETE FROM %s' % table)
            monitor_db.action('VACUUM')
            return True
        except Exception as e:
            logger.error("Tautulli Database :: Failed to clear database table '%s': %s." % (table, e))
            return False


def delete_sessions():
    logger.info("Tautulli Database :: Clearing temporary sessions from database.")
    return clear_table('sessions')


def delete_recently_added():
    logger.info("Tautulli Database :: Clearing recently added items from database.")
    return clear_table('recently_added')


def delete_rows_from_table(table, row_ids):
    if row_ids and isinstance(row_ids, str):
        row_ids = list(map(cast_to_int, row_ids.split(',')))

    if row_ids:
        logger.info("Tautulli Database :: Deleting row ids %s from %s database table", row_ids, table)
        query = "DELETE FROM " + table + " WHERE id IN (%s) " % ','.join(['?'] * len(row_ids))
        monitor_db = MonitorDatabase()

        try:
            monitor_db.action(query, row_ids)
            return True
        except Exception as e:
            logger.error("Tautulli Database :: Failed to delete rows from %s database table: %s" % (table, e))
            return False

    return True


def delete_session_history_rows(row_ids=None):
    success = []
    for table in ('session_history', 'session_history_media_info', 'session_history_metadata'):
        success.append(delete_rows_from_table(table=table, row_ids=row_ids))
    return all(success)


def delete_user_history(user_id=None):
    if str(user_id).isdigit():
        monitor_db = MonitorDatabase()

        # Get all history associated with the user_id
        result = monitor_db.select('SELECT id FROM session_history WHERE user_id = ?',
                                   [user_id])
        row_ids = [row['id'] for row in result]

        logger.info("Tautulli Database :: Deleting all history for user_id %s from database." % user_id)
        return delete_session_history_rows(row_ids=row_ids)


def delete_library_history(section_id=None):
    if str(section_id).isdigit():
        monitor_db = MonitorDatabase()

        # Get all history associated with the section_id
        result = monitor_db.select('SELECT session_history.id FROM session_history '
                                   'JOIN session_history_metadata ON session_history.id = session_history_metadata.id '
                                   'WHERE session_history_metadata.section_id = ?',
                                   [section_id])
        row_ids = [row['id'] for row in result]

        logger.info("Tautulli Database :: Deleting all history for library section_id %s from database." % section_id)
        return delete_session_history_rows(row_ids=row_ids)


def db_filename(filename=FILENAME):
    """ Returns the filepath to the db """

    return os.path.join(plexpy.DATA_DIR, filename)


def make_backup(cleanup=False, scheduler=False):
    """ Makes a backup of db, removes all but the last 5 backups """

    # Check the integrity of the database first
    integrity = (integrity_check()['integrity_check'] == 'ok')

    corrupt = ''
    if not integrity:
        corrupt = '.corrupt'
        plexpy.NOTIFY_QUEUE.put({'notify_action': 'on_plexpydbcorrupt'})

    if scheduler:
        backup_file = 'tautulli.backup-{}{}.sched.db'.format(arrow.now().format('YYYYMMDDHHmmss'), corrupt)
    else:
        backup_file = 'tautulli.backup-{}{}.db'.format(arrow.now().format('YYYYMMDDHHmmss'), corrupt)
    backup_folder = plexpy.CONFIG.BACKUP_DIR
    backup_file_fp = os.path.join(backup_folder, backup_file)

    # In case the user has deleted it manually
    if not os.path.exists(backup_folder):
        os.makedirs(backup_folder)

    db = MonitorDatabase()
    db.connection.execute('BEGIN IMMEDIATE')
    shutil.copyfile(db_filename(), backup_file_fp)
    db.connection.rollback()

    # Only cleanup if the database integrity is okay
    if cleanup and integrity:
        now = time.time()
        # Delete all scheduled backup older than BACKUP_DAYS.
        for root, dirs, files in os.walk(backup_folder):
            db_files = [os.path.join(root, f) for f in files if f.endswith('.sched.db')]
            for file_ in db_files:
                if os.stat(file_).st_mtime < now - plexpy.CONFIG.BACKUP_DAYS * 86400:
                    try:
                        os.remove(file_)
                    except OSError as e:
                        logger.error("Tautulli Database :: Failed to delete %s from the backup folder: %s" % (file_, e))

    if backup_file in os.listdir(backup_folder):
        logger.debug("Tautulli Database :: Successfully backed up %s to %s" % (db_filename(), backup_file))
        return True
    else:
        logger.error("Tautulli Database :: Failed to backup %s to %s" % (db_filename(), backup_file))
        return False


def get_cache_size():
    # This will protect against typecasting problems produced by empty string and None settings
    if not plexpy.CONFIG.CACHE_SIZEMB:
        # sqlite will work with this (very slowly)
        return 0
    return int(plexpy.CONFIG.CACHE_SIZEMB)


def dict_factory(cursor, row):
    d = {}
    for idx, col in enumerate(cursor.description):
        d[col[0]] = row[idx]

    return d


class MonitorDatabase(object):

    def __init__(self, filename=FILENAME):
        self.filename = filename
        self.connection = sqlite3.connect(db_filename(filename), timeout=20)
        # Set database synchronous mode (default NORMAL)
        self.connection.execute("PRAGMA synchronous = %s" % plexpy.CONFIG.SYNCHRONOUS_MODE)
        # Set database journal mode (default WAL)
        self.connection.execute("PRAGMA journal_mode = %s" % plexpy.CONFIG.JOURNAL_MODE)
        # Set database cache size (default 32MB)
        self.connection.execute("PRAGMA cache_size = -%s" % (get_cache_size() * 1024))
        self.connection.row_factory = dict_factory

    def action(self, query, args=None, return_last_id=False):
        if query is None:
            return

        with db_lock:
            sql_result = None
            attempts = 0

            while attempts < 5:
                try:
                    with self.connection as c:
                        if args is None:
                            sql_result = c.execute(query)
                        else:
                            sql_result = c.execute(query, args)
                    # Our transaction was successful, leave the loop
                    break

                except sqlite3.OperationalError as e:
                    if "unable to open database file" in e or "database is locked" in e:
                        logger.warn("Tautulli Database :: Database Error: %s", e)
                        attempts += 1
                        time.sleep(1)
                    else:
                        logger.error("Tautulli Database :: Database error: %s", e)
                        raise

                except sqlite3.DatabaseError as e:
                    logger.error("Tautulli Database :: Fatal Error executing %s :: %s", query, e)
                    raise

            return sql_result

    def select(self, query, args=None):

        sql_results = self.action(query, args).fetchall()

        if sql_results is None or sql_results == [None]:
            return []

        return sql_results

    def select_single(self, query, args=None):

        sql_results = self.action(query, args).fetchone()

        if sql_results is None or sql_results == "":
            return {}

        return sql_results

    def upsert(self, table_name, value_dict, key_dict):

        trans_type = 'update'
        changes_before = self.connection.total_changes

        gen_params = lambda my_dict: [x + " = ?" for x in my_dict]

        update_query = "UPDATE " + table_name + " SET " + ", ".join(gen_params(value_dict)) + \
                       " WHERE " + " AND ".join(gen_params(key_dict))

        self.action(update_query, list(value_dict.values()) + list(key_dict.values()))

        if self.connection.total_changes == changes_before:
            trans_type = 'insert'
            insert_query = (
                "INSERT INTO " + table_name + " (" + ", ".join(list(value_dict.keys()) + list(key_dict.keys())) + ")" +
                " VALUES (" + ", ".join(["?"] * len(list(value_dict.keys()) + list(key_dict.keys()))) + ")"
            )
            try:
                self.action(insert_query, list(value_dict.values()) + list(key_dict.values()))
            except sqlite3.IntegrityError:
                logger.info("Tautulli Database :: Queries failed: %s and %s", update_query, insert_query)

        # We want to know if it was an update or insert
        return trans_type

    def last_insert_id(self):
        # Get the last insert row id
        result = self.select_single(query='SELECT last_insert_rowid() AS last_id')
        if result:
            return result.get('last_id', None)