import logging
import pymysql
from typing import List, Tuple, Dict, Optional

class MariaDB:
    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        db_name: str,
        debug: bool = False,
    ) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.db_name = db_name
        self.debug = debug
        self._ensure_db_and_tables()

    def _conn(self, db: Optional[str] = None):
        return pymysql.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self.password,
            database=db or None,
            autocommit=True,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
        )

    def _ensure_db_and_tables(self) -> None:
        try:
            with self._conn() as con:
                with con.cursor() as cur:
                    cur.execute(f"CREATE DATABASE IF NOT EXISTS `{self.db_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;")
                    if self.debug:
                        logging.debug(f"DB create/check rc={cur.rowcount}")
            with self._conn(self.db_name) as con:
                with con.cursor() as cur:
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS cpu_total (
                            ts DATETIME NOT NULL,
                            percent FLOAT NOT NULL,
                            PRIMARY KEY (ts)
                        ) ENGINE=InnoDB;
                    """)
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS cpu_core (
                            ts DATETIME NOT NULL,
                            core_index INT NOT NULL,
                            percent FLOAT NOT NULL,
                            PRIMARY KEY (ts, core_index)
                        ) ENGINE=InnoDB;
                    """)
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS cpu_stats (
                            ts DATETIME NOT NULL PRIMARY KEY,
                            freq_mhz FLOAT,
                            fan_rpm FLOAT,
                            package_temp_c FLOAT
                        ) ENGINE=InnoDB;
                    """)
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS net_io (
                            ts DATETIME NOT NULL PRIMARY KEY,
                            up_bps BIGINT,
                            down_bps BIGINT
                        ) ENGINE=InnoDB;
                    """)
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS disk_io (
                            ts DATETIME NOT NULL PRIMARY KEY,
                            read_bps BIGINT,
                            write_bps BIGINT
                        ) ENGINE=InnoDB;
                    """)
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS sys_info (
                            ts DATETIME NOT NULL PRIMARY KEY,
                            os_version VARCHAR(255),
                            cpu_model VARCHAR(255),
                            ram_total BIGINT,
                            vram_total BIGINT,
                            disk_total BIGINT
                        ) ENGINE=InnoDB;
                    """)
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS sw_versions (
                            ts DATETIME NOT NULL PRIMARY KEY,
                            nginx VARCHAR(255),
                            java VARCHAR(255),
                            python_cuda VARCHAR(255)
                        ) ENGINE=InnoDB;
                    """)
                    cur.execute("""
                        CREATE TABLE IF NOT EXISTS process_status (
                            ts DATETIME NOT NULL,
                            proc_name VARCHAR(128) NOT NULL,
                            instances INT,
                            cpu_percent FLOAT,
                            mem_rss BIGINT,
                            PRIMARY KEY (ts, proc_name)
                        ) ENGINE=InnoDB;
                    """)
                    if self.debug:
                        logging.debug("Tables ensured")
        except Exception as e:
            logging.error(f"初始化数据库/表失败: {e}")
            raise

    def insert_many(self, sql: str, rows: List[Tuple]) -> None:
        if not rows:
            return
        try:
            with self._conn(self.db_name) as con:
                with con.cursor() as cur:
                    cur.executemany(sql, rows)
                    if self.debug:
                        logging.debug(f"executemany rc={cur.rowcount} rows_in={len(rows)}")
        except Exception as e:
            logging.error(f"DB insert_many 失败: {e} sql={sql} rows={len(rows)}")
            raise

    def insert_one(self, sql: str, row: Tuple) -> None:
        try:
            with self._conn(self.db_name) as con:
                with con.cursor() as cur:
                    cur.execute(sql, row)
                    if self.debug:
                        logging.debug(f"execute rc={cur.rowcount}")
        except Exception as e:
            logging.error(f"DB insert_one 失败: {e} sql={sql} row={row}")
            raise

    def query(self, sql: str, params: Tuple = ()) -> List[Dict]:
        try:
            with self._conn(self.db_name) as con:
                with con.cursor() as cur:
                    cur.execute(sql, params)
                    rows = cur.fetchall()
                    if self.debug:
                        logging.debug(f"query rows={len(rows)}")
                    return rows
        except Exception as e:
            logging.error(f"DB query 失败: {e} sql={sql} params={params}")
            raise

    def wipe_all(self) -> None:
        try:
            with self._conn(self.db_name) as con:
                with con.cursor() as cur:
                    for tbl in ["cpu_total", "cpu_core", "cpu_stats", "net_io", "disk_io", "sys_info", "sw_versions", "process_status"]:
                        cur.execute(f"TRUNCATE TABLE {tbl};")
                    if self.debug:
                        logging.debug("All tables truncated")
        except Exception as e:
            logging.error(f"TRUNCATE 失败: {e}")
            raise

    def purge_older_than(self, minutes: int) -> None:
        if minutes is None or minutes <= 0:
            return
        try:
            with self._conn(self.db_name) as con:
                with con.cursor() as cur:
                    for tbl in ["cpu_total", "cpu_core", "cpu_stats", "net_io", "disk_io", "sys_info", "sw_versions", "process_status"]:
                        cur.execute(f"DELETE FROM {tbl} WHERE ts < (UTC_TIMESTAMP() - INTERVAL {int(minutes)} MINUTE);")
                        if self.debug:
                            logging.debug(f"purge({minutes}m) {tbl} rc={cur.rowcount}")
        except Exception as e:
            logging.error(f"数据清理失败({minutes}m): {e}")
            raise