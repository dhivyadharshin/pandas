"""SQL io tests

The SQL tests are broken down in different classes:

- `PandasSQLTest`: base class with common methods for all test classes
- Tests for the public API (only tests with sqlite3)
    - `_TestSQLApi` base class
    - `TestSQLApi`: test the public API with sqlalchemy engine
    - `TestSQLiteFallbackApi`: test the public API with a sqlite DBAPI
      connection
- Tests for the different SQL flavors (flavor specific type conversions)
    - Tests for the sqlalchemy mode: `_TestSQLAlchemy` is the base class with
      common methods, `_TestSQLAlchemyConn` tests the API with a SQLAlchemy
      Connection object. The different tested flavors (sqlite3, MySQL,
      PostgreSQL) derive from the base class
    - Tests for the fallback mode (`TestSQLiteFallback`)

"""
from __future__ import annotations

import csv
from datetime import (
    date,
    datetime,
    time,
)
from io import StringIO
from pathlib import Path
import sqlite3
import warnings

import numpy as np
import pytest

from pandas.core.dtypes.common import (
    is_datetime64_dtype,
    is_datetime64tz_dtype,
)

import pandas as pd
from pandas import (
    DataFrame,
    Index,
    MultiIndex,
    Series,
    Timestamp,
    concat,
    date_range,
    isna,
    to_datetime,
    to_timedelta,
)
import pandas._testing as tm

import pandas.io.sql as sql
from pandas.io.sql import (
    SQLAlchemyEngine,
    _gt14,
    get_engine,
    read_sql_query,
    read_sql_table,
)

try:
    import sqlalchemy
    from sqlalchemy import inspect
    from sqlalchemy.ext import declarative
    from sqlalchemy.orm import session as sa_session
    import sqlalchemy.schema
    import sqlalchemy.sql.sqltypes as sqltypes

    SQLALCHEMY_INSTALLED = True
except ImportError:
    SQLALCHEMY_INSTALLED = False

SQL_STRINGS = {
    "read_parameters": {
        "sqlite": "SELECT * FROM iris WHERE Name=? AND SepalLength=?",
        "mysql": "SELECT * FROM iris WHERE `Name`=%s AND `SepalLength`=%s",
        "postgresql": 'SELECT * FROM iris WHERE "Name"=%s AND "SepalLength"=%s',
    },
    "read_named_parameters": {
        "sqlite": """
                SELECT * FROM iris WHERE Name=:name AND SepalLength=:length
                """,
        "mysql": """
                SELECT * FROM iris WHERE
                `Name`=%(name)s AND `SepalLength`=%(length)s
                """,
        "postgresql": """
                SELECT * FROM iris WHERE
                "Name"=%(name)s AND "SepalLength"=%(length)s
                """,
    },
    "read_no_parameters_with_percent": {
        "sqlite": "SELECT * FROM iris WHERE Name LIKE '%'",
        "mysql": "SELECT * FROM iris WHERE `Name` LIKE '%'",
        "postgresql": "SELECT * FROM iris WHERE \"Name\" LIKE '%'",
    },
}


def iris_table_metadata(dialect: str):
    from sqlalchemy import (
        REAL,
        Column,
        Float,
        MetaData,
        String,
        Table,
    )

    dtype = Float if dialect == "postgresql" else REAL
    metadata = MetaData()
    iris = Table(
        "iris",
        metadata,
        Column("SepalLength", dtype),
        Column("SepalWidth", dtype),
        Column("PetalLength", dtype),
        Column("PetalWidth", dtype),
        Column("Name", String(200)),
    )
    return iris


def create_and_load_iris_sqlite3(conn: sqlite3.Connection, iris_file: Path):
    cur = conn.cursor()
    stmt = """CREATE TABLE iris (
            "SepalLength" REAL,
            "SepalWidth" REAL,
            "PetalLength" REAL,
            "PetalWidth" REAL,
            "Name" TEXT
        )"""
    cur.execute(stmt)
    with iris_file.open(newline=None) as csvfile:
        reader = csv.reader(csvfile)
        next(reader)
        stmt = "INSERT INTO iris VALUES(?, ?, ?, ?, ?)"
        cur.executemany(stmt, reader)


def create_and_load_iris(conn, iris_file: Path, dialect: str):
    from sqlalchemy import insert
    from sqlalchemy.engine import Engine

    iris = iris_table_metadata(dialect)
    iris.drop(conn, checkfirst=True)
    iris.create(bind=conn)

    with iris_file.open(newline=None) as csvfile:
        reader = csv.reader(csvfile)
        header = next(reader)
        params = [{key: value for key, value in zip(header, row)} for row in reader]
        stmt = insert(iris).values(params)
        if isinstance(conn, Engine):
            with conn.connect() as conn:
                conn.execute(stmt)
        else:
            conn.execute(stmt)


def create_and_load_iris_view(conn):
    stmt = "CREATE VIEW iris_view AS SELECT * FROM iris"
    if isinstance(conn, sqlite3.Connection):
        cur = conn.cursor()
        cur.execute(stmt)
    else:
        from sqlalchemy import text
        from sqlalchemy.engine import Engine

        stmt = text(stmt)
        if isinstance(conn, Engine):
            with conn.connect() as conn:
                conn.execute(stmt)
        else:
            conn.execute(stmt)


def types_table_metadata(dialect: str):
    from sqlalchemy import (
        TEXT,
        Boolean,
        Column,
        DateTime,
        Float,
        Integer,
        MetaData,
        Table,
    )

    date_type = TEXT if dialect == "sqlite" else DateTime
    bool_type = Integer if dialect == "sqlite" else Boolean
    metadata = MetaData()
    types = Table(
        "types",
        metadata,
        Column("TextCol", TEXT),
        Column("DateCol", date_type),
        Column("IntDateCol", Integer),
        Column("IntDateOnlyCol", Integer),
        Column("FloatCol", Float),
        Column("IntCol", Integer),
        Column("BoolCol", bool_type),
        Column("IntColWithNull", Integer),
        Column("BoolColWithNull", bool_type),
    )
    if dialect == "postgresql":
        types.append_column(Column("DateColWithTz", DateTime(timezone=True)))
    return types


def create_and_load_types_sqlite3(conn: sqlite3.Connection, types_data: list[dict]):
    cur = conn.cursor()
    stmt = """CREATE TABLE types (
                    "TextCol" TEXT,
                    "DateCol" TEXT,
                    "IntDateCol" INTEGER,
                    "IntDateOnlyCol" INTEGER,
                    "FloatCol" REAL,
                    "IntCol" INTEGER,
                    "BoolCol" INTEGER,
                    "IntColWithNull" INTEGER,
                    "BoolColWithNull" INTEGER
                )"""
    cur.execute(stmt)

    stmt = """
            INSERT INTO types
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
    cur.executemany(stmt, types_data)


def create_and_load_types(conn, types_data: list[dict], dialect: str):
    from sqlalchemy import insert
    from sqlalchemy.engine import Engine

    types = types_table_metadata(dialect)
    types.drop(conn, checkfirst=True)
    types.create(bind=conn)

    stmt = insert(types).values(types_data)
    if isinstance(conn, Engine):
        with conn.connect() as conn:
            conn.execute(stmt)
    else:
        conn.execute(stmt)


@pytest.fixture
def iris_path(datapath):
    iris_path = datapath("io", "data", "csv", "iris.csv")
    return Path(iris_path)


@pytest.fixture
def types_data():
    return [
        {
            "TextCol": "first",
            "DateCol": "2000-01-03 00:00:00",
            "IntDateCol": 535852800,
            "IntDateOnlyCol": 20101010,
            "FloatCol": 10.10,
            "IntCol": 1,
            "BoolCol": False,
            "IntColWithNull": 1,
            "BoolColWithNull": False,
            "DateColWithTz": "2000-01-01 00:00:00-08:00",
        },
        {
            "TextCol": "first",
            "DateCol": "2000-01-04 00:00:00",
            "IntDateCol": 1356998400,
            "IntDateOnlyCol": 20101212,
            "FloatCol": 10.10,
            "IntCol": 1,
            "BoolCol": False,
            "IntColWithNull": None,
            "BoolColWithNull": None,
            "DateColWithTz": "2000-06-01 00:00:00-07:00",
        },
    ]


@pytest.fixture
def types_data_frame(types_data):
    dtypes = {
        "TextCol": "str",
        "DateCol": "str",
        "IntDateCol": "int64",
        "IntDateOnlyCol": "int64",
        "FloatCol": "float",
        "IntCol": "int64",
        "BoolCol": "int64",
        "IntColWithNull": "float",
        "BoolColWithNull": "float",
    }
    df = DataFrame(types_data)
    return df[dtypes.keys()].astype(dtypes)


@pytest.fixture
def test_frame1():
    columns = ["index", "A", "B", "C", "D"]
    data = [
        (
            "2000-01-03 00:00:00",
            0.980268513777,
            3.68573087906,
            -0.364216805298,
            -1.15973806169,
        ),
        (
            "2000-01-04 00:00:00",
            1.04791624281,
            -0.0412318367011,
            -0.16181208307,
            0.212549316967,
        ),
        (
            "2000-01-05 00:00:00",
            0.498580885705,
            0.731167677815,
            -0.537677223318,
            1.34627041952,
        ),
        (
            "2000-01-06 00:00:00",
            1.12020151869,
            1.56762092543,
            0.00364077397681,
            0.67525259227,
        ),
    ]
    return DataFrame(data, columns=columns)


@pytest.fixture
def test_frame3():
    columns = ["index", "A", "B"]
    data = [
        ("2000-01-03 00:00:00", 2 ** 31 - 1, -1.987670),
        ("2000-01-04 00:00:00", -29, -0.0412318367011),
        ("2000-01-05 00:00:00", 20000, 0.731167677815),
        ("2000-01-06 00:00:00", -290867, 1.56762092543),
    ]
    return DataFrame(data, columns=columns)


class MixInBase:
    def teardown_method(self, method):
        # if setup fails, there may not be a connection to close.
        if hasattr(self, "conn"):
            for tbl in self._get_all_tables():
                self.drop_table(tbl)
            self._close_conn()


class MySQLMixIn(MixInBase):
    def drop_table(self, table_name):
        cur = self.conn.cursor()
        cur.execute(f"DROP TABLE IF EXISTS {sql._get_valid_mysql_name(table_name)}")
        self.conn.commit()

    def _get_all_tables(self):
        cur = self.conn.cursor()
        cur.execute("SHOW TABLES")
        return [table[0] for table in cur.fetchall()]

    def _close_conn(self):
        from pymysql.err import Error

        try:
            self.conn.close()
        except Error:
            pass


class SQLiteMixIn(MixInBase):
    def drop_table(self, table_name):
        self.conn.execute(
            f"DROP TABLE IF EXISTS {sql._get_valid_sqlite_name(table_name)}"
        )
        self.conn.commit()

    def _get_all_tables(self):
        c = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        return [table[0] for table in c.fetchall()]

    def _close_conn(self):
        self.conn.close()


class SQLAlchemyMixIn(MixInBase):
    def drop_table(self, table_name):
        sql.SQLDatabase(self.conn).drop_table(table_name)

    def _get_all_tables(self):
        meta = sqlalchemy.schema.MetaData(bind=self.conn)
        meta.reflect()
        table_list = meta.tables.keys()
        return table_list

    def _close_conn(self):
        # https://docs.sqlalchemy.org/en/13/core/connections.html#engine-disposal
        self.conn.dispose()


class PandasSQLTest:
    """
    Base class with common private methods for SQLAlchemy and fallback cases.

    """

    def _get_exec(self):
        if hasattr(self.conn, "execute"):
            return self.conn
        else:
            return self.conn.cursor()

    @pytest.fixture
    def load_iris_data(self, iris_path):
        if not hasattr(self, "conn"):
            self.setup_connect()
        self.drop_table("iris")
        if isinstance(self.conn, sqlite3.Connection):
            create_and_load_iris_sqlite3(self.conn, iris_path)
        else:
            create_and_load_iris(self.conn, iris_path, self.flavor)

    @pytest.fixture
    def load_types_data(self, types_data):
        if not hasattr(self, "conn"):
            self.setup_connect()
        if self.flavor != "postgresql":
            for entry in types_data:
                entry.pop("DateColWithTz")
        if isinstance(self.conn, sqlite3.Connection):
            types_data = [tuple(entry.values()) for entry in types_data]
            create_and_load_types_sqlite3(self.conn, types_data)
        else:
            create_and_load_types(self.conn, types_data, self.flavor)

    def _check_iris_loaded_frame(self, iris_frame):
        pytype = iris_frame.dtypes[0].type
        row = iris_frame.iloc[0]

        assert issubclass(pytype, np.floating)
        tm.equalContents(row.values, [5.1, 3.5, 1.4, 0.2, "Iris-setosa"])

    def _count_rows(self, table_name):
        result = (
            self._get_exec()
            .execute(f"SELECT count(*) AS count_1 FROM {table_name}")
            .fetchone()
        )
        return result[0]

    def _read_sql_iris(self):
        iris_frame = self.pandasSQL.read_query("SELECT * FROM iris")
        self._check_iris_loaded_frame(iris_frame)

    def _read_sql_iris_parameter(self):
        query = SQL_STRINGS["read_parameters"][self.flavor]
        params = ["Iris-setosa", 5.1]
        iris_frame = self.pandasSQL.read_query(query, params=params)
        self._check_iris_loaded_frame(iris_frame)

    def _read_sql_iris_named_parameter(self):
        query = SQL_STRINGS["read_named_parameters"][self.flavor]
        params = {"name": "Iris-setosa", "length": 5.1}
        iris_frame = self.pandasSQL.read_query(query, params=params)
        self._check_iris_loaded_frame(iris_frame)

    def _read_sql_iris_no_parameter_with_percent(self):
        query = SQL_STRINGS["read_no_parameters_with_percent"][self.flavor]
        iris_frame = self.pandasSQL.read_query(query, params=None)
        self._check_iris_loaded_frame(iris_frame)

    def _to_sql(self, test_frame1, method=None):
        self.drop_table("test_frame1")

        self.pandasSQL.to_sql(test_frame1, "test_frame1", method=method)
        assert self.pandasSQL.has_table("test_frame1")

        num_entries = len(test_frame1)
        num_rows = self._count_rows("test_frame1")
        assert num_rows == num_entries

        # Nuke table
        self.drop_table("test_frame1")

    def _to_sql_empty(self, test_frame1):
        self.drop_table("test_frame1")
        self.pandasSQL.to_sql(test_frame1.iloc[:0], "test_frame1")

    def _to_sql_fail(self, test_frame1):
        self.drop_table("test_frame1")

        self.pandasSQL.to_sql(test_frame1, "test_frame1", if_exists="fail")
        assert self.pandasSQL.has_table("test_frame1")

        msg = "Table 'test_frame1' already exists"
        with pytest.raises(ValueError, match=msg):
            self.pandasSQL.to_sql(test_frame1, "test_frame1", if_exists="fail")

        self.drop_table("test_frame1")

    def _to_sql_replace(self, test_frame1):
        self.drop_table("test_frame1")

        self.pandasSQL.to_sql(test_frame1, "test_frame1", if_exists="fail")
        # Add to table again
        self.pandasSQL.to_sql(test_frame1, "test_frame1", if_exists="replace")
        assert self.pandasSQL.has_table("test_frame1")

        num_entries = len(test_frame1)
        num_rows = self._count_rows("test_frame1")

        assert num_rows == num_entries
        self.drop_table("test_frame1")

    def _to_sql_append(self, test_frame1):
        # Nuke table just in case
        self.drop_table("test_frame1")

        self.pandasSQL.to_sql(test_frame1, "test_frame1", if_exists="fail")

        # Add to table again
        self.pandasSQL.to_sql(test_frame1, "test_frame1", if_exists="append")
        assert self.pandasSQL.has_table("test_frame1")

        num_entries = 2 * len(test_frame1)
        num_rows = self._count_rows("test_frame1")

        assert num_rows == num_entries
        self.drop_table("test_frame1")

    def _to_sql_method_callable(self, test_frame1):
        check = []  # used to double check function below is really being used

        def sample(pd_table, conn, keys, data_iter):
            check.append(1)
            data = [dict(zip(keys, row)) for row in data_iter]
            conn.execute(pd_table.table.insert(), data)

        self.drop_table("test_frame1")

        self.pandasSQL.to_sql(test_frame1, "test_frame1", method=sample)
        assert self.pandasSQL.has_table("test_frame1")

        assert check == [1]
        num_entries = len(test_frame1)
        num_rows = self._count_rows("test_frame1")
        assert num_rows == num_entries
        # Nuke table
        self.drop_table("test_frame1")

    def _to_sql_with_sql_engine(self, test_frame1, engine="auto", **engine_kwargs):
        """`to_sql` with the `engine` param"""
        # mostly copied from this class's `_to_sql()` method
        self.drop_table("test_frame1")

        self.pandasSQL.to_sql(
            test_frame1, "test_frame1", engine=engine, **engine_kwargs
        )
        assert self.pandasSQL.has_table("test_frame1")

        num_entries = len(test_frame1)
        num_rows = self._count_rows("test_frame1")
        assert num_rows == num_entries

        # Nuke table
        self.drop_table("test_frame1")

    def _roundtrip(self, test_frame1):
        self.drop_table("test_frame_roundtrip")
        self.pandasSQL.to_sql(test_frame1, "test_frame_roundtrip")
        result = self.pandasSQL.read_query("SELECT * FROM test_frame_roundtrip")

        result.set_index("level_0", inplace=True)
        # result.index.astype(int)

        result.index.name = None

        tm.assert_frame_equal(result, test_frame1)

    def _execute_sql(self):
        # drop_sql = "DROP TABLE IF EXISTS test"  # should already be done
        iris_results = self.pandasSQL.execute("SELECT * FROM iris")
        row = iris_results.fetchone()
        tm.equalContents(row, [5.1, 3.5, 1.4, 0.2, "Iris-setosa"])

    def _to_sql_save_index(self):
        df = DataFrame.from_records(
            [(1, 2.1, "line1"), (2, 1.5, "line2")], columns=["A", "B", "C"], index=["A"]
        )
        self.pandasSQL.to_sql(df, "test_to_sql_saves_index")
        ix_cols = self._get_index_columns("test_to_sql_saves_index")
        assert ix_cols == [["A"]]

    def _transaction_test(self):
        with self.pandasSQL.run_transaction() as trans:
            trans.execute("CREATE TABLE test_trans (A INT, B TEXT)")

        class DummyException(Exception):
            pass

        # Make sure when transaction is rolled back, no rows get inserted
        ins_sql = "INSERT INTO test_trans (A,B) VALUES (1, 'blah')"
        try:
            with self.pandasSQL.run_transaction() as trans:
                trans.execute(ins_sql)
                raise DummyException("error")
        except DummyException:
            # ignore raised exception
            pass
        res = self.pandasSQL.read_query("SELECT * FROM test_trans")
        assert len(res) == 0

        # Make sure when transaction is committed, rows do get inserted
        with self.pandasSQL.run_transaction() as trans:
            trans.execute(ins_sql)
        res2 = self.pandasSQL.read_query("SELECT * FROM test_trans")
        assert len(res2) == 1


# -----------------------------------------------------------------------------
# -- Testing the public API


class _TestSQLApi(PandasSQLTest):
    """
    Base class to test the public API.

    From this two classes are derived to run these tests for both the
    sqlalchemy mode (`TestSQLApi`) and the fallback mode
    (`TestSQLiteFallbackApi`).  These tests are run with sqlite3. Specific
    tests for the different sql flavours are included in `_TestSQLAlchemy`.

    Notes:
    flavor can always be passed even in SQLAlchemy mode,
    should be correctly ignored.

    we don't use drop_table because that isn't part of the public api

    """

    flavor = "sqlite"
    mode: str

    def setup_connect(self):
        self.conn = self.connect()

    @pytest.fixture(autouse=True)
    def setup_method(self, load_iris_data, load_types_data):
        self.load_test_data_and_sql()

    def load_test_data_and_sql(self):
        create_and_load_iris_view(self.conn)

    def test_read_sql_iris(self):
        iris_frame = sql.read_sql_query("SELECT * FROM iris", self.conn)
        self._check_iris_loaded_frame(iris_frame)

    def test_read_sql_view(self):
        iris_frame = sql.read_sql_query("SELECT * FROM iris_view", self.conn)
        self._check_iris_loaded_frame(iris_frame)

    def test_read_sql_with_chunksize_no_result(self):
        query = "SELECT * FROM iris_view WHERE SepalLength < 0.0"
        with_batch = sql.read_sql_query(query, self.conn, chunksize=5)
        without_batch = sql.read_sql_query(query, self.conn)
        tm.assert_frame_equal(concat(with_batch), without_batch)

    def test_to_sql(self, test_frame1):
        sql.to_sql(test_frame1, "test_frame1", self.conn)
        assert sql.has_table("test_frame1", self.conn)

    def test_to_sql_fail(self, test_frame1):
        sql.to_sql(test_frame1, "test_frame2", self.conn, if_exists="fail")
        assert sql.has_table("test_frame2", self.conn)

        msg = "Table 'test_frame2' already exists"
        with pytest.raises(ValueError, match=msg):
            sql.to_sql(test_frame1, "test_frame2", self.conn, if_exists="fail")

    def test_to_sql_replace(self, test_frame1):
        sql.to_sql(test_frame1, "test_frame3", self.conn, if_exists="fail")
        # Add to table again
        sql.to_sql(test_frame1, "test_frame3", self.conn, if_exists="replace")
        assert sql.has_table("test_frame3", self.conn)

        num_entries = len(test_frame1)
        num_rows = self._count_rows("test_frame3")

        assert num_rows == num_entries

    def test_to_sql_append(self, test_frame1):
        sql.to_sql(test_frame1, "test_frame4", self.conn, if_exists="fail")

        # Add to table again
        sql.to_sql(test_frame1, "test_frame4", self.conn, if_exists="append")
        assert sql.has_table("test_frame4", self.conn)

        num_entries = 2 * len(test_frame1)
        num_rows = self._count_rows("test_frame4")

        assert num_rows == num_entries

    def test_to_sql_type_mapping(self, test_frame3):
        sql.to_sql(test_frame3, "test_frame5", self.conn, index=False)
        result = sql.read_sql("SELECT * FROM test_frame5", self.conn)

        tm.assert_frame_equal(test_frame3, result)

    def test_to_sql_series(self):
        s = Series(np.arange(5, dtype="int64"), name="series")
        sql.to_sql(s, "test_series", self.conn, index=False)
        s2 = sql.read_sql_query("SELECT * FROM test_series", self.conn)
        tm.assert_frame_equal(s.to_frame(), s2)

    def test_roundtrip(self, test_frame1):
        sql.to_sql(test_frame1, "test_frame_roundtrip", con=self.conn)
        result = sql.read_sql_query("SELECT * FROM test_frame_roundtrip", con=self.conn)

        # HACK!
        result.index = test_frame1.index
        result.set_index("level_0", inplace=True)
        result.index.astype(int)
        result.index.name = None
        tm.assert_frame_equal(result, test_frame1)

    def test_roundtrip_chunksize(self, test_frame1):
        sql.to_sql(
            test_frame1,
            "test_frame_roundtrip",
            con=self.conn,
            index=False,
            chunksize=2,
        )
        result = sql.read_sql_query("SELECT * FROM test_frame_roundtrip", con=self.conn)
        tm.assert_frame_equal(result, test_frame1)

    def test_execute_sql(self):
        # drop_sql = "DROP TABLE IF EXISTS test"  # should already be done
        iris_results = sql.execute("SELECT * FROM iris", con=self.conn)
        row = iris_results.fetchone()
        tm.equalContents(row, [5.1, 3.5, 1.4, 0.2, "Iris-setosa"])

    def test_date_parsing(self):
        # Test date parsing in read_sql
        # No Parsing
        df = sql.read_sql_query("SELECT * FROM types", self.conn)
        assert not issubclass(df.DateCol.dtype.type, np.datetime64)

        df = sql.read_sql_query(
            "SELECT * FROM types", self.conn, parse_dates=["DateCol"]
        )
        assert issubclass(df.DateCol.dtype.type, np.datetime64)
        assert df.DateCol.tolist() == [
            Timestamp(2000, 1, 3, 0, 0, 0),
            Timestamp(2000, 1, 4, 0, 0, 0),
        ]

        df = sql.read_sql_query(
            "SELECT * FROM types",
            self.conn,
            parse_dates={"DateCol": "%Y-%m-%d %H:%M:%S"},
        )
        assert issubclass(df.DateCol.dtype.type, np.datetime64)
        assert df.DateCol.tolist() == [
            Timestamp(2000, 1, 3, 0, 0, 0),
            Timestamp(2000, 1, 4, 0, 0, 0),
        ]

        df = sql.read_sql_query(
            "SELECT * FROM types", self.conn, parse_dates=["IntDateCol"]
        )
        assert issubclass(df.IntDateCol.dtype.type, np.datetime64)
        assert df.IntDateCol.tolist() == [
            Timestamp(1986, 12, 25, 0, 0, 0),
            Timestamp(2013, 1, 1, 0, 0, 0),
        ]

        df = sql.read_sql_query(
            "SELECT * FROM types", self.conn, parse_dates={"IntDateCol": "s"}
        )
        assert issubclass(df.IntDateCol.dtype.type, np.datetime64)
        assert df.IntDateCol.tolist() == [
            Timestamp(1986, 12, 25, 0, 0, 0),
            Timestamp(2013, 1, 1, 0, 0, 0),
        ]

        df = sql.read_sql_query(
            "SELECT * FROM types",
            self.conn,
            parse_dates={"IntDateOnlyCol": "%Y%m%d"},
        )
        assert issubclass(df.IntDateOnlyCol.dtype.type, np.datetime64)
        assert df.IntDateOnlyCol.tolist() == [
            Timestamp("2010-10-10"),
            Timestamp("2010-12-12"),
        ]

    @pytest.mark.parametrize("error", ["ignore", "raise", "coerce"])
    @pytest.mark.parametrize(
        "read_sql, text, mode",
        [
            (sql.read_sql, "SELECT * FROM types", ("sqlalchemy", "fallback")),
            (sql.read_sql, "types", ("sqlalchemy")),
            (
                sql.read_sql_query,
                "SELECT * FROM types",
                ("sqlalchemy", "fallback"),
            ),
            (sql.read_sql_table, "types", ("sqlalchemy")),
        ],
    )
    def test_custom_dateparsing_error(
        self, read_sql, text, mode, error, types_data_frame
    ):
        if self.mode in mode:
            expected = types_data_frame.astype({"DateCol": "datetime64[ns]"})

            result = read_sql(
                text,
                con=self.conn,
                parse_dates={
                    "DateCol": {"errors": error},
                },
            )

            tm.assert_frame_equal(result, expected)

    def test_date_and_index(self):
        # Test case where same column appears in parse_date and index_col

        df = sql.read_sql_query(
            "SELECT * FROM types",
            self.conn,
            index_col="DateCol",
            parse_dates=["DateCol", "IntDateCol"],
        )

        assert issubclass(df.index.dtype.type, np.datetime64)
        assert issubclass(df.IntDateCol.dtype.type, np.datetime64)

    def test_timedelta(self):

        # see #6921
        df = to_timedelta(Series(["00:00:01", "00:00:03"], name="foo")).to_frame()
        with tm.assert_produces_warning(UserWarning):
            df.to_sql("test_timedelta", self.conn)
        result = sql.read_sql_query("SELECT * FROM test_timedelta", self.conn)
        tm.assert_series_equal(result["foo"], df["foo"].view("int64"))

    def test_complex_raises(self):
        df = DataFrame({"a": [1 + 1j, 2j]})
        msg = "Complex datatypes not supported"
        with pytest.raises(ValueError, match=msg):
            df.to_sql("test_complex", self.conn)

    @pytest.mark.parametrize(
        "index_name,index_label,expected",
        [
            # no index name, defaults to 'index'
            (None, None, "index"),
            # specifying index_label
            (None, "other_label", "other_label"),
            # using the index name
            ("index_name", None, "index_name"),
            # has index name, but specifying index_label
            ("index_name", "other_label", "other_label"),
            # index name is integer
            (0, None, "0"),
            # index name is None but index label is integer
            (None, 0, "0"),
        ],
    )
    def test_to_sql_index_label(self, index_name, index_label, expected):
        temp_frame = DataFrame({"col1": range(4)})
        temp_frame.index.name = index_name
        query = "SELECT * FROM test_index_label"
        sql.to_sql(temp_frame, "test_index_label", self.conn, index_label=index_label)
        frame = sql.read_sql_query(query, self.conn)
        assert frame.columns[0] == expected

    def test_to_sql_index_label_multiindex(self):
        temp_frame = DataFrame(
            {"col1": range(4)},
            index=MultiIndex.from_product([("A0", "A1"), ("B0", "B1")]),
        )

        # no index name, defaults to 'level_0' and 'level_1'
        sql.to_sql(temp_frame, "test_index_label", self.conn)
        frame = sql.read_sql_query("SELECT * FROM test_index_label", self.conn)
        assert frame.columns[0] == "level_0"
        assert frame.columns[1] == "level_1"

        # specifying index_label
        sql.to_sql(
            temp_frame,
            "test_index_label",
            self.conn,
            if_exists="replace",
            index_label=["A", "B"],
        )
        frame = sql.read_sql_query("SELECT * FROM test_index_label", self.conn)
        assert frame.columns[:2].tolist() == ["A", "B"]

        # using the index name
        temp_frame.index.names = ["A", "B"]
        sql.to_sql(temp_frame, "test_index_label", self.conn, if_exists="replace")
        frame = sql.read_sql_query("SELECT * FROM test_index_label", self.conn)
        assert frame.columns[:2].tolist() == ["A", "B"]

        # has index name, but specifying index_label
        sql.to_sql(
            temp_frame,
            "test_index_label",
            self.conn,
            if_exists="replace",
            index_label=["C", "D"],
        )
        frame = sql.read_sql_query("SELECT * FROM test_index_label", self.conn)
        assert frame.columns[:2].tolist() == ["C", "D"]

        msg = "Length of 'index_label' should match number of levels, which is 2"
        with pytest.raises(ValueError, match=msg):
            sql.to_sql(
                temp_frame,
                "test_index_label",
                self.conn,
                if_exists="replace",
                index_label="C",
            )

    def test_multiindex_roundtrip(self):
        df = DataFrame.from_records(
            [(1, 2.1, "line1"), (2, 1.5, "line2")],
            columns=["A", "B", "C"],
            index=["A", "B"],
        )

        df.to_sql("test_multiindex_roundtrip", self.conn)
        result = sql.read_sql_query(
            "SELECT * FROM test_multiindex_roundtrip", self.conn, index_col=["A", "B"]
        )
        tm.assert_frame_equal(df, result, check_index_type=True)

    @pytest.mark.parametrize(
        "dtype",
        [
            None,
            int,
            float,
            {"A": int, "B": float},
        ],
    )
    def test_dtype_argument(self, dtype):
        # GH10285 Add dtype argument to read_sql_query
        df = DataFrame([[1.2, 3.4], [5.6, 7.8]], columns=["A", "B"])
        df.to_sql("test_dtype_argument", self.conn)

        expected = df.astype(dtype)
        result = sql.read_sql_query(
            "SELECT A, B FROM test_dtype_argument", con=self.conn, dtype=dtype
        )

        tm.assert_frame_equal(result, expected)

    def test_integer_col_names(self):
        df = DataFrame([[1, 2], [3, 4]], columns=[0, 1])
        sql.to_sql(df, "test_frame_integer_col_names", self.conn, if_exists="replace")

    def test_get_schema(self, test_frame1):
        create_sql = sql.get_schema(test_frame1, "test", con=self.conn)
        assert "CREATE" in create_sql

    def test_get_schema_with_schema(self, test_frame1):
        # GH28486
        create_sql = sql.get_schema(test_frame1, "test", con=self.conn, schema="pypi")
        assert "CREATE TABLE pypi." in create_sql

    def test_get_schema_dtypes(self):
        float_frame = DataFrame({"a": [1.1, 1.2], "b": [2.1, 2.2]})
        dtype = sqlalchemy.Integer if self.mode == "sqlalchemy" else "INTEGER"
        create_sql = sql.get_schema(
            float_frame, "test", con=self.conn, dtype={"b": dtype}
        )
        assert "CREATE" in create_sql
        assert "INTEGER" in create_sql

    def test_get_schema_keys(self, test_frame1):
        frame = DataFrame({"Col1": [1.1, 1.2], "Col2": [2.1, 2.2]})
        create_sql = sql.get_schema(frame, "test", con=self.conn, keys="Col1")
        constraint_sentence = 'CONSTRAINT test_pk PRIMARY KEY ("Col1")'
        assert constraint_sentence in create_sql

        # multiple columns as key (GH10385)
        create_sql = sql.get_schema(test_frame1, "test", con=self.conn, keys=["A", "B"])
        constraint_sentence = 'CONSTRAINT test_pk PRIMARY KEY ("A", "B")'
        assert constraint_sentence in create_sql

    def test_chunksize_read(self):
        df = DataFrame(np.random.randn(22, 5), columns=list("abcde"))
        df.to_sql("test_chunksize", self.conn, index=False)

        # reading the query in one time
        res1 = sql.read_sql_query("select * from test_chunksize", self.conn)

        # reading the query in chunks with read_sql_query
        res2 = DataFrame()
        i = 0
        sizes = [5, 5, 5, 5, 2]

        for chunk in sql.read_sql_query(
            "select * from test_chunksize", self.conn, chunksize=5
        ):
            res2 = concat([res2, chunk], ignore_index=True)
            assert len(chunk) == sizes[i]
            i += 1

        tm.assert_frame_equal(res1, res2)

        # reading the query in chunks with read_sql_query
        if self.mode == "sqlalchemy":
            res3 = DataFrame()
            i = 0
            sizes = [5, 5, 5, 5, 2]

            for chunk in sql.read_sql_table("test_chunksize", self.conn, chunksize=5):
                res3 = concat([res3, chunk], ignore_index=True)
                assert len(chunk) == sizes[i]
                i += 1

            tm.assert_frame_equal(res1, res3)

    def test_categorical(self):
        # GH8624
        # test that categorical gets written correctly as dense column
        df = DataFrame(
            {
                "person_id": [1, 2, 3],
                "person_name": ["John P. Doe", "Jane Dove", "John P. Doe"],
            }
        )
        df2 = df.copy()
        df2["person_name"] = df2["person_name"].astype("category")

        df2.to_sql("test_categorical", self.conn, index=False)
        res = sql.read_sql_query("SELECT * FROM test_categorical", self.conn)

        tm.assert_frame_equal(res, df)

    def test_unicode_column_name(self):
        # GH 11431
        df = DataFrame([[1, 2], [3, 4]], columns=["\xe9", "b"])
        df.to_sql("test_unicode", self.conn, index=False)

    def test_escaped_table_name(self):
        # GH 13206
        df = DataFrame({"A": [0, 1, 2], "B": [0.2, np.nan, 5.6]})
        df.to_sql("d1187b08-4943-4c8d-a7f6", self.conn, index=False)

        res = sql.read_sql_query("SELECT * FROM `d1187b08-4943-4c8d-a7f6`", self.conn)

        tm.assert_frame_equal(res, df)


@pytest.mark.single
@pytest.mark.skipif(not SQLALCHEMY_INSTALLED, reason="SQLAlchemy not installed")
class TestSQLApi(SQLAlchemyMixIn, _TestSQLApi):
    """
    Test the public API as it would be used directly

    Tests for `read_sql_table` are included here, as this is specific for the
    sqlalchemy mode.

    """

    flavor = "sqlite"
    mode = "sqlalchemy"

    def connect(self):
        return sqlalchemy.create_engine("sqlite:///:memory:")

    def test_read_table_columns(self, test_frame1):
        # test columns argument in read_table
        sql.to_sql(test_frame1, "test_frame", self.conn)

        cols = ["A", "B"]
        result = sql.read_sql_table("test_frame", self.conn, columns=cols)
        assert result.columns.tolist() == cols

    def test_read_table_index_col(self, test_frame1):
        # test columns argument in read_table
        sql.to_sql(test_frame1, "test_frame", self.conn)

        result = sql.read_sql_table("test_frame", self.conn, index_col="index")
        assert result.index.names == ["index"]

        result = sql.read_sql_table("test_frame", self.conn, index_col=["A", "B"])
        assert result.index.names == ["A", "B"]

        result = sql.read_sql_table(
            "test_frame", self.conn, index_col=["A", "B"], columns=["C", "D"]
        )
        assert result.index.names == ["A", "B"]
        assert result.columns.tolist() == ["C", "D"]

    def test_read_sql_delegate(self):
        iris_frame1 = sql.read_sql_query("SELECT * FROM iris", self.conn)
        iris_frame2 = sql.read_sql("SELECT * FROM iris", self.conn)
        tm.assert_frame_equal(iris_frame1, iris_frame2)

        iris_frame1 = sql.read_sql_table("iris", self.conn)
        iris_frame2 = sql.read_sql("iris", self.conn)
        tm.assert_frame_equal(iris_frame1, iris_frame2)

    def test_not_reflect_all_tables(self):
        # create invalid table
        qry = """CREATE TABLE invalid (x INTEGER, y UNKNOWN);"""
        self.conn.execute(qry)
        qry = """CREATE TABLE other_table (x INTEGER, y INTEGER);"""
        self.conn.execute(qry)

        with warnings.catch_warnings(record=True) as w:
            # Cause all warnings to always be triggered.
            warnings.simplefilter("always")
            # Trigger a warning.
            sql.read_sql_table("other_table", self.conn)
            sql.read_sql_query("SELECT * FROM other_table", self.conn)
            # Verify some things
            assert len(w) == 0

    def test_warning_case_insensitive_table_name(self, test_frame1):
        # see gh-7815
        #
        # We can't test that this warning is triggered, a the database
        # configuration would have to be altered. But here we test that
        # the warning is certainly NOT triggered in a normal case.
        with warnings.catch_warnings(record=True) as w:
            # Cause all warnings to always be triggered.
            warnings.simplefilter("always")
            # This should not trigger a Warning
            test_frame1.to_sql("CaseSensitive", self.conn)
            # Verify some things
            assert len(w) == 0

    def _get_index_columns(self, tbl_name):
        from sqlalchemy.engine import reflection

        insp = reflection.Inspector.from_engine(self.conn)
        ixs = insp.get_indexes("test_index_saved")
        ixs = [i["column_names"] for i in ixs]
        return ixs

    def test_sqlalchemy_type_mapping(self):

        # Test Timestamp objects (no datetime64 because of timezone) (GH9085)
        df = DataFrame(
            {"time": to_datetime(["201412120154", "201412110254"], utc=True)}
        )
        db = sql.SQLDatabase(self.conn)
        table = sql.SQLTable("test_type", db, frame=df)
        # GH 9086: TIMESTAMP is the suggested type for datetimes with timezones
        assert isinstance(table.table.c["time"].type, sqltypes.TIMESTAMP)

    @pytest.mark.parametrize(
        "integer, expected",
        [
            ("int8", "SMALLINT"),
            ("Int8", "SMALLINT"),
            ("uint8", "SMALLINT"),
            ("UInt8", "SMALLINT"),
            ("int16", "SMALLINT"),
            ("Int16", "SMALLINT"),
            ("uint16", "INTEGER"),
            ("UInt16", "INTEGER"),
            ("int32", "INTEGER"),
            ("Int32", "INTEGER"),
            ("uint32", "BIGINT"),
            ("UInt32", "BIGINT"),
            ("int64", "BIGINT"),
            ("Int64", "BIGINT"),
            (int, "BIGINT" if np.dtype(int).name == "int64" else "INTEGER"),
        ],
    )
    def test_sqlalchemy_integer_mapping(self, integer, expected):
        # GH35076 Map pandas integer to optimal SQLAlchemy integer type
        df = DataFrame([0, 1], columns=["a"], dtype=integer)
        db = sql.SQLDatabase(self.conn)
        table = sql.SQLTable("test_type", db, frame=df)

        result = str(table.table.c.a.type)
        assert result == expected

    @pytest.mark.parametrize("integer", ["uint64", "UInt64"])
    def test_sqlalchemy_integer_overload_mapping(self, integer):
        # GH35076 Map pandas integer to optimal SQLAlchemy integer type
        df = DataFrame([0, 1], columns=["a"], dtype=integer)
        db = sql.SQLDatabase(self.conn)
        with pytest.raises(
            ValueError, match="Unsigned 64 bit integer datatype is not supported"
        ):
            sql.SQLTable("test_type", db, frame=df)

    def test_database_uri_string(self, test_frame1):
        # Test read_sql and .to_sql method with a database URI (GH10654)
        # db_uri = 'sqlite:///:memory:' # raises
        # sqlalchemy.exc.OperationalError: (sqlite3.OperationalError) near
        # "iris": syntax error [SQL: 'iris']
        with tm.ensure_clean() as name:
            db_uri = "sqlite:///" + name
            table = "iris"
            test_frame1.to_sql(table, db_uri, if_exists="replace", index=False)
            test_frame2 = sql.read_sql(table, db_uri)
            test_frame3 = sql.read_sql_table(table, db_uri)
            query = "SELECT * FROM iris"
            test_frame4 = sql.read_sql_query(query, db_uri)
        tm.assert_frame_equal(test_frame1, test_frame2)
        tm.assert_frame_equal(test_frame1, test_frame3)
        tm.assert_frame_equal(test_frame1, test_frame4)

        # using driver that will not be installed on Travis to trigger error
        # in sqlalchemy.create_engine -> test passing of this error to user
        try:
            # the rest of this test depends on pg8000's being absent
            import pg8000  # noqa

            pytest.skip("pg8000 is installed")
        except ImportError:
            pass

        db_uri = "postgresql+pg8000://user:pass@host/dbname"
        with pytest.raises(ImportError, match="pg8000"):
            sql.read_sql("select * from table", db_uri)

    def test_query_by_text_obj(self):
        # WIP : GH10846
        name_text = sqlalchemy.text("select * from iris where name=:name")
        iris_df = sql.read_sql(name_text, self.conn, params={"name": "Iris-versicolor"})
        all_names = set(iris_df["Name"])
        assert all_names == {"Iris-versicolor"}

    def test_query_by_select_obj(self):
        # WIP : GH10846
        iris = iris_table_metadata(self.flavor)

        name_select = sqlalchemy.select([iris]).where(
            iris.c.Name == sqlalchemy.bindparam("name")
        )
        iris_df = sql.read_sql(name_select, self.conn, params={"name": "Iris-setosa"})
        all_names = set(iris_df["Name"])
        assert all_names == {"Iris-setosa"}

    def test_column_with_percentage(self):
        # GH 37157
        df = DataFrame({"A": [0, 1, 2], "%_variation": [3, 4, 5]})
        df.to_sql("test_column_percentage", self.conn, index=False)

        res = sql.read_sql_table("test_column_percentage", self.conn)

        tm.assert_frame_equal(res, df)


class _EngineToConnMixin:
    """
    A mixin that causes setup_connect to create a conn rather than an engine.
    """

    @pytest.fixture(autouse=True)
    def setup_method(self, load_iris_data, load_types_data):
        super().load_test_data_and_sql()
        engine = self.conn
        conn = engine.connect()
        self.__tx = conn.begin()
        self.pandasSQL = sql.SQLDatabase(conn)
        self.__engine = engine
        self.conn = conn

        yield

        self.__tx.rollback()
        self.conn.close()
        self.conn = self.__engine
        self.pandasSQL = sql.SQLDatabase(self.__engine)


@pytest.mark.single
class TestSQLApiConn(_EngineToConnMixin, TestSQLApi):
    pass


@pytest.mark.single
class TestSQLiteFallbackApi(SQLiteMixIn, _TestSQLApi):
    """
    Test the public sqlite connection fallback API

    """

    flavor = "sqlite"
    mode = "fallback"

    def connect(self, database=":memory:"):
        return sqlite3.connect(database)

    def test_sql_open_close(self, test_frame3):
        # Test if the IO in the database still work if the connection closed
        # between the writing and reading (as in many real situations).

        with tm.ensure_clean() as name:

            conn = self.connect(name)
            sql.to_sql(test_frame3, "test_frame3_legacy", conn, index=False)
            conn.close()

            conn = self.connect(name)
            result = sql.read_sql_query("SELECT * FROM test_frame3_legacy;", conn)
            conn.close()

        tm.assert_frame_equal(test_frame3, result)

    @pytest.mark.skipif(SQLALCHEMY_INSTALLED, reason="SQLAlchemy is installed")
    def test_con_string_import_error(self):
        conn = "mysql://root@localhost/pandas"
        with pytest.raises(ImportError, match="SQLAlchemy"):
            sql.read_sql("SELECT * FROM iris", conn)

    def test_read_sql_delegate(self):
        iris_frame1 = sql.read_sql_query("SELECT * FROM iris", self.conn)
        iris_frame2 = sql.read_sql("SELECT * FROM iris", self.conn)
        tm.assert_frame_equal(iris_frame1, iris_frame2)

        msg = "Execution failed on sql 'iris': near \"iris\": syntax error"
        with pytest.raises(sql.DatabaseError, match=msg):
            sql.read_sql("iris", self.conn)

    def test_safe_names_warning(self):
        # GH 6798
        df = DataFrame([[1, 2], [3, 4]], columns=["a", "b "])  # has a space
        # warns on create table with spaces in names
        with tm.assert_produces_warning():
            sql.to_sql(df, "test_frame3_legacy", self.conn, index=False)

    def test_get_schema2(self, test_frame1):
        # without providing a connection object (available for backwards comp)
        create_sql = sql.get_schema(test_frame1, "test")
        assert "CREATE" in create_sql

    def _get_sqlite_column_type(self, schema, column):

        for col in schema.split("\n"):
            if col.split()[0].strip('""') == column:
                return col.split()[1]
        raise ValueError(f"Column {column} not found")

    def test_sqlite_type_mapping(self):

        # Test Timestamp objects (no datetime64 because of timezone) (GH9085)
        df = DataFrame(
            {"time": to_datetime(["201412120154", "201412110254"], utc=True)}
        )
        db = sql.SQLiteDatabase(self.conn)
        table = sql.SQLiteTable("test_type", db, frame=df)
        schema = table.sql_schema()
        assert self._get_sqlite_column_type(schema, "time") == "TIMESTAMP"


# -----------------------------------------------------------------------------
# -- Database flavor specific tests


class _TestSQLAlchemy(SQLAlchemyMixIn, PandasSQLTest):
    """
    Base class for testing the sqlalchemy backend.

    Subclasses for specific database types are created below. Tests that
    deviate for each flavor are overwritten there.

    """

    flavor: str

    @pytest.fixture(autouse=True, scope="class")
    def setup_class(cls):
        cls.setup_import()
        cls.setup_driver()
        conn = cls.conn = cls.connect()
        conn.connect()

    def load_test_data_and_sql(self):
        pass

    @pytest.fixture(autouse=True)
    def setup_method(self, load_iris_data, load_types_data):
        pass

    @classmethod
    def setup_import(cls):
        # Skip this test if SQLAlchemy not available
        if not SQLALCHEMY_INSTALLED:
            pytest.skip("SQLAlchemy not installed")

    @classmethod
    def setup_driver(cls):
        raise NotImplementedError()

    @classmethod
    def connect(cls):
        raise NotImplementedError()

    def setup_connect(self):
        try:
            self.conn = self.connect()
            self.pandasSQL = sql.SQLDatabase(self.conn)
            # to test if connection can be made:
            self.conn.connect()
        except sqlalchemy.exc.OperationalError:
            pytest.skip(f"Can't connect to {self.flavor} server")

    def test_read_sql(self):
        self._read_sql_iris()

    def test_read_sql_parameter(self):
        self._read_sql_iris_parameter()

    def test_read_sql_named_parameter(self):
        self._read_sql_iris_named_parameter()

    def test_to_sql(self, test_frame1):
        self._to_sql(test_frame1)

    def test_to_sql_empty(self, test_frame1):
        self._to_sql_empty(test_frame1)

    def test_to_sql_fail(self, test_frame1):
        self._to_sql_fail(test_frame1)

    def test_to_sql_replace(self, test_frame1):
        self._to_sql_replace(test_frame1)

    def test_to_sql_append(self, test_frame1):
        self._to_sql_append(test_frame1)

    def test_to_sql_method_multi(self, test_frame1):
        self._to_sql(test_frame1, method="multi")

    def test_to_sql_method_callable(self, test_frame1):
        self._to_sql_method_callable(test_frame1)

    def test_create_table(self):
        temp_conn = self.connect()
        temp_frame = DataFrame(
            {"one": [1.0, 2.0, 3.0, 4.0], "two": [4.0, 3.0, 2.0, 1.0]}
        )

        pandasSQL = sql.SQLDatabase(temp_conn)
        pandasSQL.to_sql(temp_frame, "temp_frame")

        if _gt14():
            insp = inspect(temp_conn)
            assert insp.has_table("temp_frame")
        else:
            assert temp_conn.has_table("temp_frame")

    def test_drop_table(self):
        temp_conn = self.connect()

        temp_frame = DataFrame(
            {"one": [1.0, 2.0, 3.0, 4.0], "two": [4.0, 3.0, 2.0, 1.0]}
        )

        pandasSQL = sql.SQLDatabase(temp_conn)
        pandasSQL.to_sql(temp_frame, "temp_frame")

        if _gt14():
            insp = inspect(temp_conn)
            assert insp.has_table("temp_frame")
        else:
            assert temp_conn.has_table("temp_frame")

        pandasSQL.drop_table("temp_frame")

        if _gt14():
            assert not insp.has_table("temp_frame")
        else:
            assert not temp_conn.has_table("temp_frame")

    def test_roundtrip(self, test_frame1):
        self._roundtrip(test_frame1)

    def test_execute_sql(self):
        self._execute_sql()

    def test_read_table(self):
        iris_frame = sql.read_sql_table("iris", con=self.conn)
        self._check_iris_loaded_frame(iris_frame)

    def test_read_table_columns(self):
        iris_frame = sql.read_sql_table(
            "iris", con=self.conn, columns=["SepalLength", "SepalLength"]
        )
        tm.equalContents(iris_frame.columns.values, ["SepalLength", "SepalLength"])

    def test_read_table_absent_raises(self):
        msg = "Table this_doesnt_exist not found"
        with pytest.raises(ValueError, match=msg):
            sql.read_sql_table("this_doesnt_exist", con=self.conn)

    def test_default_type_conversion(self):
        df = sql.read_sql_table("types", self.conn)

        assert issubclass(df.FloatCol.dtype.type, np.floating)
        assert issubclass(df.IntCol.dtype.type, np.integer)
        assert issubclass(df.BoolCol.dtype.type, np.bool_)

        # Int column with NA values stays as float
        assert issubclass(df.IntColWithNull.dtype.type, np.floating)
        # Bool column with NA values becomes object
        assert issubclass(df.BoolColWithNull.dtype.type, object)

    def test_bigint(self):
        # int64 should be converted to BigInteger, GH7433
        df = DataFrame(data={"i64": [2 ** 62]})
        df.to_sql("test_bigint", self.conn, index=False)
        result = sql.read_sql_table("test_bigint", self.conn)

        tm.assert_frame_equal(df, result)

    def test_default_date_load(self):
        df = sql.read_sql_table("types", self.conn)

        # IMPORTANT - sqlite has no native date type, so shouldn't parse, but
        # MySQL SHOULD be converted.
        assert issubclass(df.DateCol.dtype.type, np.datetime64)

    def test_datetime_with_timezone(self):
        # edge case that converts postgresql datetime with time zone types
        # to datetime64[ns,psycopg2.tz.FixedOffsetTimezone..], which is ok
        # but should be more natural, so coerce to datetime64[ns] for now

        def check(col):
            # check that a column is either datetime64[ns]
            # or datetime64[ns, UTC]
            if is_datetime64_dtype(col.dtype):

                # "2000-01-01 00:00:00-08:00" should convert to
                # "2000-01-01 08:00:00"
                assert col[0] == Timestamp("2000-01-01 08:00:00")

                # "2000-06-01 00:00:00-07:00" should convert to
                # "2000-06-01 07:00:00"
                assert col[1] == Timestamp("2000-06-01 07:00:00")

            elif is_datetime64tz_dtype(col.dtype):
                assert str(col.dt.tz) == "UTC"

                # "2000-01-01 00:00:00-08:00" should convert to
                # "2000-01-01 08:00:00"
                # "2000-06-01 00:00:00-07:00" should convert to
                # "2000-06-01 07:00:00"
                # GH 6415
                expected_data = [
                    Timestamp("2000-01-01 08:00:00", tz="UTC"),
                    Timestamp("2000-06-01 07:00:00", tz="UTC"),
                ]
                expected = Series(expected_data, name=col.name)
                tm.assert_series_equal(col, expected)

            else:
                raise AssertionError(
                    f"DateCol loaded with incorrect type -> {col.dtype}"
                )

        # GH11216
        df = read_sql_query("select * from types", self.conn)
        if not hasattr(df, "DateColWithTz"):
            pytest.skip("no column with datetime with time zone")

        # this is parsed on Travis (linux), but not on macosx for some reason
        # even with the same versions of psycopg2 & sqlalchemy, possibly a
        # Postgresql server version difference
        col = df.DateColWithTz
        assert is_datetime64tz_dtype(col.dtype)

        df = read_sql_query(
            "select * from types", self.conn, parse_dates=["DateColWithTz"]
        )
        if not hasattr(df, "DateColWithTz"):
            pytest.skip("no column with datetime with time zone")
        col = df.DateColWithTz
        assert is_datetime64tz_dtype(col.dtype)
        assert str(col.dt.tz) == "UTC"
        check(df.DateColWithTz)

        df = concat(
            list(read_sql_query("select * from types", self.conn, chunksize=1)),
            ignore_index=True,
        )
        col = df.DateColWithTz
        assert is_datetime64tz_dtype(col.dtype)
        assert str(col.dt.tz) == "UTC"
        expected = sql.read_sql_table("types", self.conn)
        col = expected.DateColWithTz
        assert is_datetime64tz_dtype(col.dtype)
        tm.assert_series_equal(df.DateColWithTz, expected.DateColWithTz)

        # xref #7139
        # this might or might not be converted depending on the postgres driver
        df = sql.read_sql_table("types", self.conn)
        check(df.DateColWithTz)

    def test_datetime_with_timezone_roundtrip(self):
        # GH 9086
        # Write datetimetz data to a db and read it back
        # For dbs that support timestamps with timezones, should get back UTC
        # otherwise naive data should be returned
        expected = DataFrame(
            {"A": date_range("2013-01-01 09:00:00", periods=3, tz="US/Pacific")}
        )
        expected.to_sql("test_datetime_tz", self.conn, index=False)

        if self.flavor == "postgresql":
            # SQLAlchemy "timezones" (i.e. offsets) are coerced to UTC
            expected["A"] = expected["A"].dt.tz_convert("UTC")
        else:
            # Otherwise, timestamps are returned as local, naive
            expected["A"] = expected["A"].dt.tz_localize(None)

        result = sql.read_sql_table("test_datetime_tz", self.conn)
        tm.assert_frame_equal(result, expected)

        result = sql.read_sql_query("SELECT * FROM test_datetime_tz", self.conn)
        if self.flavor == "sqlite":
            # read_sql_query does not return datetime type like read_sql_table
            assert isinstance(result.loc[0, "A"], str)
            result["A"] = to_datetime(result["A"])
        tm.assert_frame_equal(result, expected)

    def test_out_of_bounds_datetime(self):
        # GH 26761
        data = DataFrame({"date": datetime(9999, 1, 1)}, index=[0])
        data.to_sql("test_datetime_obb", self.conn, index=False)
        result = sql.read_sql_table("test_datetime_obb", self.conn)
        expected = DataFrame([pd.NaT], columns=["date"])
        tm.assert_frame_equal(result, expected)

    def test_naive_datetimeindex_roundtrip(self):
        # GH 23510
        # Ensure that a naive DatetimeIndex isn't converted to UTC
        dates = date_range("2018-01-01", periods=5, freq="6H")._with_freq(None)
        expected = DataFrame({"nums": range(5)}, index=dates)
        expected.to_sql("foo_table", self.conn, index_label="info_date")
        result = sql.read_sql_table("foo_table", self.conn, index_col="info_date")
        # result index with gain a name from a set_index operation; expected
        tm.assert_frame_equal(result, expected, check_names=False)

    def test_date_parsing(self):
        # No Parsing
        df = sql.read_sql_table("types", self.conn)
        expected_type = object if self.flavor == "sqlite" else np.datetime64
        assert issubclass(df.DateCol.dtype.type, expected_type)

        df = sql.read_sql_table("types", self.conn, parse_dates=["DateCol"])
        assert issubclass(df.DateCol.dtype.type, np.datetime64)

        df = sql.read_sql_table(
            "types", self.conn, parse_dates={"DateCol": "%Y-%m-%d %H:%M:%S"}
        )
        assert issubclass(df.DateCol.dtype.type, np.datetime64)

        df = sql.read_sql_table(
            "types",
            self.conn,
            parse_dates={"DateCol": {"format": "%Y-%m-%d %H:%M:%S"}},
        )
        assert issubclass(df.DateCol.dtype.type, np.datetime64)

        df = sql.read_sql_table("types", self.conn, parse_dates=["IntDateCol"])
        assert issubclass(df.IntDateCol.dtype.type, np.datetime64)

        df = sql.read_sql_table("types", self.conn, parse_dates={"IntDateCol": "s"})
        assert issubclass(df.IntDateCol.dtype.type, np.datetime64)

        df = sql.read_sql_table(
            "types", self.conn, parse_dates={"IntDateCol": {"unit": "s"}}
        )
        assert issubclass(df.IntDateCol.dtype.type, np.datetime64)

    def test_datetime(self):
        df = DataFrame(
            {"A": date_range("2013-01-01 09:00:00", periods=3), "B": np.arange(3.0)}
        )
        df.to_sql("test_datetime", self.conn)

        # with read_table -> type information from schema used
        result = sql.read_sql_table("test_datetime", self.conn)
        result = result.drop("index", axis=1)
        tm.assert_frame_equal(result, df)

        # with read_sql -> no type information -> sqlite has no native
        result = sql.read_sql_query("SELECT * FROM test_datetime", self.conn)
        result = result.drop("index", axis=1)
        if self.flavor == "sqlite":
            assert isinstance(result.loc[0, "A"], str)
            result["A"] = to_datetime(result["A"])
            tm.assert_frame_equal(result, df)
        else:
            tm.assert_frame_equal(result, df)

    def test_datetime_NaT(self):
        df = DataFrame(
            {"A": date_range("2013-01-01 09:00:00", periods=3), "B": np.arange(3.0)}
        )
        df.loc[1, "A"] = np.nan
        df.to_sql("test_datetime", self.conn, index=False)

        # with read_table -> type information from schema used
        result = sql.read_sql_table("test_datetime", self.conn)
        tm.assert_frame_equal(result, df)

        # with read_sql -> no type information -> sqlite has no native
        result = sql.read_sql_query("SELECT * FROM test_datetime", self.conn)
        if self.flavor == "sqlite":
            assert isinstance(result.loc[0, "A"], str)
            result["A"] = to_datetime(result["A"], errors="coerce")
            tm.assert_frame_equal(result, df)
        else:
            tm.assert_frame_equal(result, df)

    def test_datetime_date(self):
        # test support for datetime.date
        df = DataFrame([date(2014, 1, 1), date(2014, 1, 2)], columns=["a"])
        df.to_sql("test_date", self.conn, index=False)
        res = read_sql_table("test_date", self.conn)
        result = res["a"]
        expected = to_datetime(df["a"])
        # comes back as datetime64
        tm.assert_series_equal(result, expected)

    def test_datetime_time(self):
        # test support for datetime.time
        df = DataFrame([time(9, 0, 0), time(9, 1, 30)], columns=["a"])
        df.to_sql("test_time", self.conn, index=False)
        res = read_sql_table("test_time", self.conn)
        tm.assert_frame_equal(res, df)

        # GH8341
        # first, use the fallback to have the sqlite adapter put in place
        sqlite_conn = TestSQLiteFallback.connect()
        sql.to_sql(df, "test_time2", sqlite_conn, index=False)
        res = sql.read_sql_query("SELECT * FROM test_time2", sqlite_conn)
        ref = df.applymap(lambda _: _.strftime("%H:%M:%S.%f"))
        tm.assert_frame_equal(ref, res)  # check if adapter is in place
        # then test if sqlalchemy is unaffected by the sqlite adapter
        sql.to_sql(df, "test_time3", self.conn, index=False)
        if self.flavor == "sqlite":
            res = sql.read_sql_query("SELECT * FROM test_time3", self.conn)
            ref = df.applymap(lambda _: _.strftime("%H:%M:%S.%f"))
            tm.assert_frame_equal(ref, res)
        res = sql.read_sql_table("test_time3", self.conn)
        tm.assert_frame_equal(df, res)

    def test_mixed_dtype_insert(self):
        # see GH6509
        s1 = Series(2 ** 25 + 1, dtype=np.int32)
        s2 = Series(0.0, dtype=np.float32)
        df = DataFrame({"s1": s1, "s2": s2})

        # write and read again
        df.to_sql("test_read_write", self.conn, index=False)
        df2 = sql.read_sql_table("test_read_write", self.conn)

        tm.assert_frame_equal(df, df2, check_dtype=False, check_exact=True)

    def test_nan_numeric(self):
        # NaNs in numeric float column
        df = DataFrame({"A": [0, 1, 2], "B": [0.2, np.nan, 5.6]})
        df.to_sql("test_nan", self.conn, index=False)

        # with read_table
        result = sql.read_sql_table("test_nan", self.conn)
        tm.assert_frame_equal(result, df)

        # with read_sql
        result = sql.read_sql_query("SELECT * FROM test_nan", self.conn)
        tm.assert_frame_equal(result, df)

    def test_nan_fullcolumn(self):
        # full NaN column (numeric float column)
        df = DataFrame({"A": [0, 1, 2], "B": [np.nan, np.nan, np.nan]})
        df.to_sql("test_nan", self.conn, index=False)

        # with read_table
        result = sql.read_sql_table("test_nan", self.conn)
        tm.assert_frame_equal(result, df)

        # with read_sql -> not type info from table -> stays None
        df["B"] = df["B"].astype("object")
        df["B"] = None
        result = sql.read_sql_query("SELECT * FROM test_nan", self.conn)
        tm.assert_frame_equal(result, df)

    def test_nan_string(self):
        # NaNs in string column
        df = DataFrame({"A": [0, 1, 2], "B": ["a", "b", np.nan]})
        df.to_sql("test_nan", self.conn, index=False)

        # NaNs are coming back as None
        df.loc[2, "B"] = None

        # with read_table
        result = sql.read_sql_table("test_nan", self.conn)
        tm.assert_frame_equal(result, df)

        # with read_sql
        result = sql.read_sql_query("SELECT * FROM test_nan", self.conn)
        tm.assert_frame_equal(result, df)

    def _get_index_columns(self, tbl_name):
        from sqlalchemy import inspect

        insp = inspect(self.conn)

        ixs = insp.get_indexes(tbl_name)
        ixs = [i["column_names"] for i in ixs]
        return ixs

    def test_to_sql_save_index(self):
        self._to_sql_save_index()

    def test_transactions(self):
        self._transaction_test()

    def test_get_schema_create_table(self, test_frame3):
        # Use a dataframe without a bool column, since MySQL converts bool to
        # TINYINT (which read_sql_table returns as an int and causes a dtype
        # mismatch)

        tbl = "test_get_schema_create_table"
        create_sql = sql.get_schema(test_frame3, tbl, con=self.conn)
        blank_test_df = test_frame3.iloc[:0]

        self.drop_table(tbl)
        self.conn.execute(create_sql)
        returned_df = sql.read_sql_table(tbl, self.conn)
        tm.assert_frame_equal(returned_df, blank_test_df, check_index_type=False)
        self.drop_table(tbl)

    def test_dtype(self):
        cols = ["A", "B"]
        data = [(0.8, True), (0.9, None)]
        df = DataFrame(data, columns=cols)
        df.to_sql("dtype_test", self.conn)
        df.to_sql("dtype_test2", self.conn, dtype={"B": sqlalchemy.TEXT})
        meta = sqlalchemy.schema.MetaData(bind=self.conn)
        meta.reflect()
        sqltype = meta.tables["dtype_test2"].columns["B"].type
        assert isinstance(sqltype, sqlalchemy.TEXT)
        msg = "The type of B is not a SQLAlchemy type"
        with pytest.raises(ValueError, match=msg):
            df.to_sql("error", self.conn, dtype={"B": str})

        # GH9083
        df.to_sql("dtype_test3", self.conn, dtype={"B": sqlalchemy.String(10)})
        meta.reflect()
        sqltype = meta.tables["dtype_test3"].columns["B"].type
        assert isinstance(sqltype, sqlalchemy.String)
        assert sqltype.length == 10

        # single dtype
        df.to_sql("single_dtype_test", self.conn, dtype=sqlalchemy.TEXT)
        meta = sqlalchemy.schema.MetaData(bind=self.conn)
        meta.reflect()
        sqltypea = meta.tables["single_dtype_test"].columns["A"].type
        sqltypeb = meta.tables["single_dtype_test"].columns["B"].type
        assert isinstance(sqltypea, sqlalchemy.TEXT)
        assert isinstance(sqltypeb, sqlalchemy.TEXT)

    def test_notna_dtype(self):
        cols = {
            "Bool": Series([True, None]),
            "Date": Series([datetime(2012, 5, 1), None]),
            "Int": Series([1, None], dtype="object"),
            "Float": Series([1.1, None]),
        }
        df = DataFrame(cols)

        tbl = "notna_dtype_test"
        df.to_sql(tbl, self.conn)
        returned_df = sql.read_sql_table(tbl, self.conn)  # noqa
        meta = sqlalchemy.schema.MetaData(bind=self.conn)
        meta.reflect()
        if self.flavor == "mysql":
            my_type = sqltypes.Integer
        else:
            my_type = sqltypes.Boolean

        col_dict = meta.tables[tbl].columns

        assert isinstance(col_dict["Bool"].type, my_type)
        assert isinstance(col_dict["Date"].type, sqltypes.DateTime)
        assert isinstance(col_dict["Int"].type, sqltypes.Integer)
        assert isinstance(col_dict["Float"].type, sqltypes.Float)

    def test_double_precision(self):
        V = 1.23456789101112131415

        df = DataFrame(
            {
                "f32": Series([V], dtype="float32"),
                "f64": Series([V], dtype="float64"),
                "f64_as_f32": Series([V], dtype="float64"),
                "i32": Series([5], dtype="int32"),
                "i64": Series([5], dtype="int64"),
            }
        )

        df.to_sql(
            "test_dtypes",
            self.conn,
            index=False,
            if_exists="replace",
            dtype={"f64_as_f32": sqlalchemy.Float(precision=23)},
        )
        res = sql.read_sql_table("test_dtypes", self.conn)

        # check precision of float64
        assert np.round(df["f64"].iloc[0], 14) == np.round(res["f64"].iloc[0], 14)

        # check sql types
        meta = sqlalchemy.schema.MetaData(bind=self.conn)
        meta.reflect()
        col_dict = meta.tables["test_dtypes"].columns
        assert str(col_dict["f32"].type) == str(col_dict["f64_as_f32"].type)
        assert isinstance(col_dict["f32"].type, sqltypes.Float)
        assert isinstance(col_dict["f64"].type, sqltypes.Float)
        assert isinstance(col_dict["i32"].type, sqltypes.Integer)
        assert isinstance(col_dict["i64"].type, sqltypes.BigInteger)

    def test_connectable_issue_example(self):
        # This tests the example raised in issue
        # https://github.com/pandas-dev/pandas/issues/10104

        def foo(connection):
            query = "SELECT test_foo_data FROM test_foo_data"
            return sql.read_sql_query(query, con=connection)

        def bar(connection, data):
            data.to_sql(name="test_foo_data", con=connection, if_exists="append")

        def main(connectable):
            with connectable.connect() as conn:
                with conn.begin():
                    if _gt14():
                        # https://github.com/sqlalchemy/sqlalchemy/commit/
                        #  00b5c10846e800304caa86549ab9da373b42fa5d#r48323973
                        foo_data = foo(conn)
                        bar(conn, foo_data)
                    else:
                        foo_data = conn.run_callable(foo)
                        conn.run_callable(bar, foo_data)

        DataFrame({"test_foo_data": [0, 1, 2]}).to_sql("test_foo_data", self.conn)
        main(self.conn)

    @pytest.mark.parametrize(
        "input",
        [{"foo": [np.inf]}, {"foo": [-np.inf]}, {"foo": [-np.inf], "infe0": ["bar"]}],
    )
    def test_to_sql_with_negative_npinf(self, input, request):
        # GH 34431

        df = DataFrame(input)

        if self.flavor == "mysql":
            # GH 36465
            # The input {"foo": [-np.inf], "infe0": ["bar"]} does not raise any error
            # for pymysql version >= 0.10
            # TODO: remove this version check after GH 36465 is fixed
            import pymysql

            if pymysql.VERSION[0:3] >= (0, 10, 0) and "infe0" in df.columns:
                mark = pytest.mark.xfail(reason="GH 36465")
                request.node.add_marker(mark)

            msg = "inf cannot be used with MySQL"
            with pytest.raises(ValueError, match=msg):
                df.to_sql("foobar", self.conn, index=False)
        else:
            df.to_sql("foobar", self.conn, index=False)
            res = sql.read_sql_table("foobar", self.conn)
            tm.assert_equal(df, res)

    def test_temporary_table(self):
        test_data = "Hello, World!"
        expected = DataFrame({"spam": [test_data]})
        Base = declarative.declarative_base()

        class Temporary(Base):
            __tablename__ = "temp_test"
            __table_args__ = {"prefixes": ["TEMPORARY"]}
            id = sqlalchemy.Column(sqlalchemy.Integer, primary_key=True)
            spam = sqlalchemy.Column(sqlalchemy.Unicode(30), nullable=False)

        Session = sa_session.sessionmaker(bind=self.conn)
        session = Session()
        with session.transaction:
            conn = session.connection()
            Temporary.__table__.create(conn)
            session.add(Temporary(spam=test_data))
            session.flush()
            df = sql.read_sql_query(sql=sqlalchemy.select([Temporary.spam]), con=conn)

        tm.assert_frame_equal(df, expected)

    # -- SQL Engine tests (in the base class for now)
    def test_invalid_engine(self, test_frame1):
        msg = "engine must be one of 'auto', 'sqlalchemy'"
        with pytest.raises(ValueError, match=msg):
            self._to_sql_with_sql_engine(test_frame1, "bad_engine")

    def test_options_sqlalchemy(self, test_frame1):
        # use the set option
        with pd.option_context("io.sql.engine", "sqlalchemy"):
            self._to_sql_with_sql_engine(test_frame1)

    def test_options_auto(self, test_frame1):
        # use the set option
        with pd.option_context("io.sql.engine", "auto"):
            self._to_sql_with_sql_engine(test_frame1)

    def test_options_get_engine(self):
        assert isinstance(get_engine("sqlalchemy"), SQLAlchemyEngine)

        with pd.option_context("io.sql.engine", "sqlalchemy"):
            assert isinstance(get_engine("auto"), SQLAlchemyEngine)
            assert isinstance(get_engine("sqlalchemy"), SQLAlchemyEngine)

        with pd.option_context("io.sql.engine", "auto"):
            assert isinstance(get_engine("auto"), SQLAlchemyEngine)
            assert isinstance(get_engine("sqlalchemy"), SQLAlchemyEngine)

    def test_get_engine_auto_error_message(self):
        # Expect different error messages from get_engine(engine="auto")
        # if engines aren't installed vs. are installed but bad version
        pass
        # TODO fill this in when we add more engines


class _TestSQLAlchemyConn(_EngineToConnMixin, _TestSQLAlchemy):
    def test_transactions(self):
        pytest.skip("Nested transactions rollbacks don't work with Pandas")


class _TestSQLiteAlchemy:
    """
    Test the sqlalchemy backend against an in-memory sqlite database.

    """

    flavor = "sqlite"

    @classmethod
    def connect(cls):
        return sqlalchemy.create_engine("sqlite:///:memory:")

    @classmethod
    def setup_driver(cls):
        # sqlite3 is built-in
        cls.driver = None

    def test_default_type_conversion(self):
        df = sql.read_sql_table("types", self.conn)

        assert issubclass(df.FloatCol.dtype.type, np.floating)
        assert issubclass(df.IntCol.dtype.type, np.integer)

        # sqlite has no boolean type, so integer type is returned
        assert issubclass(df.BoolCol.dtype.type, np.integer)

        # Int column with NA values stays as float
        assert issubclass(df.IntColWithNull.dtype.type, np.floating)

        # Non-native Bool column with NA values stays as float
        assert issubclass(df.BoolColWithNull.dtype.type, np.floating)

    def test_default_date_load(self):
        df = sql.read_sql_table("types", self.conn)

        # IMPORTANT - sqlite has no native date type, so shouldn't parse, but
        assert not issubclass(df.DateCol.dtype.type, np.datetime64)

    def test_bigint_warning(self):
        # test no warning for BIGINT (to support int64) is raised (GH7433)
        df = DataFrame({"a": [1, 2]}, dtype="int64")
        df.to_sql("test_bigintwarning", self.conn, index=False)

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            sql.read_sql_table("test_bigintwarning", self.conn)
            assert len(w) == 0


class _TestMySQLAlchemy:
    """
    Test the sqlalchemy backend against an MySQL database.

    """

    flavor = "mysql"
    port = 3306

    @classmethod
    def connect(cls):
        return sqlalchemy.create_engine(
            f"mysql+{cls.driver}://root@localhost:{cls.port}/pandas",
            connect_args=cls.connect_args,
        )

    @classmethod
    def setup_driver(cls):
        pymysql = pytest.importorskip("pymysql")
        cls.driver = "pymysql"
        cls.connect_args = {"client_flag": pymysql.constants.CLIENT.MULTI_STATEMENTS}

    def test_default_type_conversion(self):
        df = sql.read_sql_table("types", self.conn)

        assert issubclass(df.FloatCol.dtype.type, np.floating)
        assert issubclass(df.IntCol.dtype.type, np.integer)

        # MySQL has no real BOOL type (it's an alias for TINYINT)
        assert issubclass(df.BoolCol.dtype.type, np.integer)

        # Int column with NA values stays as float
        assert issubclass(df.IntColWithNull.dtype.type, np.floating)

        # Bool column with NA = int column with NA values => becomes float
        assert issubclass(df.BoolColWithNull.dtype.type, np.floating)

    def test_read_procedure(self):
        import pymysql

        # see GH7324. Although it is more an api test, it is added to the
        # mysql tests as sqlite does not have stored procedures
        df = DataFrame({"a": [1, 2, 3], "b": [0.1, 0.2, 0.3]})
        df.to_sql("test_procedure", self.conn, index=False)

        proc = """DROP PROCEDURE IF EXISTS get_testdb;

        CREATE PROCEDURE get_testdb ()

        BEGIN
            SELECT * FROM test_procedure;
        END"""

        connection = self.conn.connect()
        trans = connection.begin()
        try:
            r1 = connection.execute(proc)  # noqa
            trans.commit()
        except pymysql.Error:
            trans.rollback()
            raise

        res1 = sql.read_sql_query("CALL get_testdb();", self.conn)
        tm.assert_frame_equal(df, res1)

        # test delegation to read_sql_query
        res2 = sql.read_sql("CALL get_testdb();", self.conn)
        tm.assert_frame_equal(df, res2)


class _TestPostgreSQLAlchemy:
    """
    Test the sqlalchemy backend against an PostgreSQL database.

    """

    flavor = "postgresql"
    port = 5432

    @classmethod
    def connect(cls):
        return sqlalchemy.create_engine(
            f"postgresql+{cls.driver}://postgres:postgres@localhost:{cls.port}/pandas"
        )

    @classmethod
    def setup_driver(cls):
        pytest.importorskip("psycopg2")
        cls.driver = "psycopg2"

    def test_schema_support(self):
        # only test this for postgresql (schema's not supported in
        # mysql/sqlite)
        df = DataFrame({"col1": [1, 2], "col2": [0.1, 0.2], "col3": ["a", "n"]})

        # create a schema
        self.conn.execute("DROP SCHEMA IF EXISTS other CASCADE;")
        self.conn.execute("CREATE SCHEMA other;")

        # write dataframe to different schema's
        df.to_sql("test_schema_public", self.conn, index=False)
        df.to_sql(
            "test_schema_public_explicit", self.conn, index=False, schema="public"
        )
        df.to_sql("test_schema_other", self.conn, index=False, schema="other")

        # read dataframes back in
        res1 = sql.read_sql_table("test_schema_public", self.conn)
        tm.assert_frame_equal(df, res1)
        res2 = sql.read_sql_table("test_schema_public_explicit", self.conn)
        tm.assert_frame_equal(df, res2)
        res3 = sql.read_sql_table(
            "test_schema_public_explicit", self.conn, schema="public"
        )
        tm.assert_frame_equal(df, res3)
        res4 = sql.read_sql_table("test_schema_other", self.conn, schema="other")
        tm.assert_frame_equal(df, res4)
        msg = "Table test_schema_other not found"
        with pytest.raises(ValueError, match=msg):
            sql.read_sql_table("test_schema_other", self.conn, schema="public")

        # different if_exists options

        # create a schema
        self.conn.execute("DROP SCHEMA IF EXISTS other CASCADE;")
        self.conn.execute("CREATE SCHEMA other;")

        # write dataframe with different if_exists options
        df.to_sql("test_schema_other", self.conn, schema="other", index=False)
        df.to_sql(
            "test_schema_other",
            self.conn,
            schema="other",
            index=False,
            if_exists="replace",
        )
        df.to_sql(
            "test_schema_other",
            self.conn,
            schema="other",
            index=False,
            if_exists="append",
        )
        res = sql.read_sql_table("test_schema_other", self.conn, schema="other")
        tm.assert_frame_equal(concat([df, df], ignore_index=True), res)

        # specifying schema in user-provided meta

        # The schema won't be applied on another Connection
        # because of transactional schemas
        if isinstance(self.conn, sqlalchemy.engine.Engine):
            engine2 = self.connect()
            pdsql = sql.SQLDatabase(engine2, schema="other")
            pdsql.to_sql(df, "test_schema_other2", index=False)
            pdsql.to_sql(df, "test_schema_other2", index=False, if_exists="replace")
            pdsql.to_sql(df, "test_schema_other2", index=False, if_exists="append")
            res1 = sql.read_sql_table("test_schema_other2", self.conn, schema="other")
            res2 = pdsql.read_table("test_schema_other2")
            tm.assert_frame_equal(res1, res2)

    def test_copy_from_callable_insertion_method(self):
        # GH 8953
        # Example in io.rst found under _io.sql.method
        # not available in sqlite, mysql
        def psql_insert_copy(table, conn, keys, data_iter):
            # gets a DBAPI connection that can provide a cursor
            dbapi_conn = conn.connection
            with dbapi_conn.cursor() as cur:
                s_buf = StringIO()
                writer = csv.writer(s_buf)
                writer.writerows(data_iter)
                s_buf.seek(0)

                columns = ", ".join([f'"{k}"' for k in keys])
                if table.schema:
                    table_name = f"{table.schema}.{table.name}"
                else:
                    table_name = table.name

                sql_query = f"COPY {table_name} ({columns}) FROM STDIN WITH CSV"
                cur.copy_expert(sql=sql_query, file=s_buf)

        expected = DataFrame({"col1": [1, 2], "col2": [0.1, 0.2], "col3": ["a", "n"]})
        expected.to_sql(
            "test_copy_insert", self.conn, index=False, method=psql_insert_copy
        )
        result = sql.read_sql_table("test_copy_insert", self.conn)
        tm.assert_frame_equal(result, expected)


@pytest.mark.single
@pytest.mark.db
class TestMySQLAlchemy(_TestMySQLAlchemy, _TestSQLAlchemy):
    pass


@pytest.mark.single
@pytest.mark.db
class TestMySQLAlchemyConn(_TestMySQLAlchemy, _TestSQLAlchemyConn):
    pass


@pytest.mark.single
@pytest.mark.db
class TestPostgreSQLAlchemy(_TestPostgreSQLAlchemy, _TestSQLAlchemy):
    pass


@pytest.mark.single
@pytest.mark.db
class TestPostgreSQLAlchemyConn(_TestPostgreSQLAlchemy, _TestSQLAlchemyConn):
    pass


@pytest.mark.single
class TestSQLiteAlchemy(_TestSQLiteAlchemy, _TestSQLAlchemy):
    pass


@pytest.mark.single
class TestSQLiteAlchemyConn(_TestSQLiteAlchemy, _TestSQLAlchemyConn):
    pass


# -----------------------------------------------------------------------------
# -- Test Sqlite / MySQL fallback


@pytest.mark.single
class TestSQLiteFallback(SQLiteMixIn, PandasSQLTest):
    """
    Test the fallback mode against an in-memory sqlite database.

    """

    flavor = "sqlite"

    @classmethod
    def connect(cls):
        return sqlite3.connect(":memory:")

    def setup_connect(self):
        self.conn = self.connect()

    @pytest.fixture(autouse=True)
    def setup_method(self, load_iris_data, load_types_data):
        self.pandasSQL = sql.SQLiteDatabase(self.conn)

    def test_read_sql(self):
        self._read_sql_iris()

    def test_read_sql_parameter(self):
        self._read_sql_iris_parameter()

    def test_read_sql_named_parameter(self):
        self._read_sql_iris_named_parameter()

    def test_to_sql(self, test_frame1):
        self._to_sql(test_frame1)

    def test_to_sql_empty(self, test_frame1):
        self._to_sql_empty(test_frame1)

    def test_to_sql_fail(self, test_frame1):
        self._to_sql_fail(test_frame1)

    def test_to_sql_replace(self, test_frame1):
        self._to_sql_replace(test_frame1)

    def test_to_sql_append(self, test_frame1):
        self._to_sql_append(test_frame1)

    def test_to_sql_method_multi(self, test_frame1):
        # GH 29921
        self._to_sql(test_frame1, method="multi")

    def test_create_and_drop_table(self):
        temp_frame = DataFrame(
            {"one": [1.0, 2.0, 3.0, 4.0], "two": [4.0, 3.0, 2.0, 1.0]}
        )

        self.pandasSQL.to_sql(temp_frame, "drop_test_frame")

        assert self.pandasSQL.has_table("drop_test_frame")

        self.pandasSQL.drop_table("drop_test_frame")

        assert not self.pandasSQL.has_table("drop_test_frame")

    def test_roundtrip(self, test_frame1):
        self._roundtrip(test_frame1)

    def test_execute_sql(self):
        self._execute_sql()

    def test_datetime_date(self):
        # test support for datetime.date
        df = DataFrame([date(2014, 1, 1), date(2014, 1, 2)], columns=["a"])
        df.to_sql("test_date", self.conn, index=False)
        res = read_sql_query("SELECT * FROM test_date", self.conn)
        if self.flavor == "sqlite":
            # comes back as strings
            tm.assert_frame_equal(res, df.astype(str))
        elif self.flavor == "mysql":
            tm.assert_frame_equal(res, df)

    def test_datetime_time(self):
        # test support for datetime.time, GH #8341
        df = DataFrame([time(9, 0, 0), time(9, 1, 30)], columns=["a"])
        df.to_sql("test_time", self.conn, index=False)
        res = read_sql_query("SELECT * FROM test_time", self.conn)
        if self.flavor == "sqlite":
            # comes back as strings
            expected = df.applymap(lambda _: _.strftime("%H:%M:%S.%f"))
            tm.assert_frame_equal(res, expected)

    def _get_index_columns(self, tbl_name):
        ixs = sql.read_sql_query(
            "SELECT * FROM sqlite_master WHERE type = 'index' "
            + f"AND tbl_name = '{tbl_name}'",
            self.conn,
        )
        ix_cols = []
        for ix_name in ixs.name:
            ix_info = sql.read_sql_query(f"PRAGMA index_info({ix_name})", self.conn)
            ix_cols.append(ix_info.name.tolist())
        return ix_cols

    def test_to_sql_save_index(self):
        self._to_sql_save_index()

    def test_transactions(self):
        self._transaction_test()

    def _get_sqlite_column_type(self, table, column):
        recs = self.conn.execute(f"PRAGMA table_info({table})")
        for cid, name, ctype, not_null, default, pk in recs:
            if name == column:
                return ctype
        raise ValueError(f"Table {table}, column {column} not found")

    def test_dtype(self):
        if self.flavor == "mysql":
            pytest.skip("Not applicable to MySQL legacy")
        cols = ["A", "B"]
        data = [(0.8, True), (0.9, None)]
        df = DataFrame(data, columns=cols)
        df.to_sql("dtype_test", self.conn)
        df.to_sql("dtype_test2", self.conn, dtype={"B": "STRING"})

        # sqlite stores Boolean values as INTEGER
        assert self._get_sqlite_column_type("dtype_test", "B") == "INTEGER"

        assert self._get_sqlite_column_type("dtype_test2", "B") == "STRING"
        msg = r"B \(<class 'bool'>\) not a string"
        with pytest.raises(ValueError, match=msg):
            df.to_sql("error", self.conn, dtype={"B": bool})

        # single dtype
        df.to_sql("single_dtype_test", self.conn, dtype="STRING")
        assert self._get_sqlite_column_type("single_dtype_test", "A") == "STRING"
        assert self._get_sqlite_column_type("single_dtype_test", "B") == "STRING"

    def test_notna_dtype(self):
        if self.flavor == "mysql":
            pytest.skip("Not applicable to MySQL legacy")

        cols = {
            "Bool": Series([True, None]),
            "Date": Series([datetime(2012, 5, 1), None]),
            "Int": Series([1, None], dtype="object"),
            "Float": Series([1.1, None]),
        }
        df = DataFrame(cols)

        tbl = "notna_dtype_test"
        df.to_sql(tbl, self.conn)

        assert self._get_sqlite_column_type(tbl, "Bool") == "INTEGER"
        assert self._get_sqlite_column_type(tbl, "Date") == "TIMESTAMP"
        assert self._get_sqlite_column_type(tbl, "Int") == "INTEGER"
        assert self._get_sqlite_column_type(tbl, "Float") == "REAL"

    def test_illegal_names(self):
        # For sqlite, these should work fine
        df = DataFrame([[1, 2], [3, 4]], columns=["a", "b"])

        msg = "Empty table or column name specified"
        with pytest.raises(ValueError, match=msg):
            df.to_sql("", self.conn)

        for ndx, weird_name in enumerate(
            [
                "test_weird_name]",
                "test_weird_name[",
                "test_weird_name`",
                'test_weird_name"',
                "test_weird_name'",
                "_b.test_weird_name_01-30",
                '"_b.test_weird_name_01-30"',
                "99beginswithnumber",
                "12345",
                "\xe9",
            ]
        ):
            df.to_sql(weird_name, self.conn)
            sql.table_exists(weird_name, self.conn)

            df2 = DataFrame([[1, 2], [3, 4]], columns=["a", weird_name])
            c_tbl = f"test_weird_col_name{ndx:d}"
            df2.to_sql(c_tbl, self.conn)
            sql.table_exists(c_tbl, self.conn)


# -----------------------------------------------------------------------------
# -- Old tests from 0.13.1 (before refactor using sqlalchemy)


_formatters = {
    datetime: "'{}'".format,
    str: "'{}'".format,
    np.str_: "'{}'".format,
    bytes: "'{}'".format,
    float: "{:.8f}".format,
    int: "{:d}".format,
    type(None): lambda x: "NULL",
    np.float64: "{:.10f}".format,
    bool: "'{!s}'".format,
}


def format_query(sql, *args):
    processed_args = []
    for arg in args:
        if isinstance(arg, float) and isna(arg):
            arg = None

        formatter = _formatters[type(arg)]
        processed_args.append(formatter(arg))

    return sql % tuple(processed_args)


def tquery(query, con=None):
    """Replace removed sql.tquery function"""
    res = sql.execute(query, con=con).fetchall()
    return None if res is None else list(res)


class TestXSQLite:
    def setup_method(self):
        self.conn = sqlite3.connect(":memory:")

    def teardown_method(self):
        self.conn.close()

    def drop_table(self, table_name):
        cur = self.conn.cursor()
        cur.execute(f"DROP TABLE IF EXISTS {sql._get_valid_sqlite_name(table_name)}")
        self.conn.commit()

    def test_basic(self):
        frame = tm.makeTimeDataFrame()
        sql.to_sql(frame, name="test_table", con=self.conn, index=False)
        result = sql.read_sql("select * from test_table", self.conn)

        # HACK! Change this once indexes are handled properly.
        result.index = frame.index

        expected = frame
        tm.assert_frame_equal(result, frame)

        frame["txt"] = ["a"] * len(frame)
        frame2 = frame.copy()
        new_idx = Index(np.arange(len(frame2))) + 10
        frame2["Idx"] = new_idx.copy()
        sql.to_sql(frame2, name="test_table2", con=self.conn, index=False)
        result = sql.read_sql("select * from test_table2", self.conn, index_col="Idx")
        expected = frame.copy()
        expected.index = new_idx
        expected.index.name = "Idx"
        tm.assert_frame_equal(expected, result)

    def test_write_row_by_row(self):
        frame = tm.makeTimeDataFrame()
        frame.iloc[0, 0] = np.nan
        create_sql = sql.get_schema(frame, "test")
        cur = self.conn.cursor()
        cur.execute(create_sql)

        ins = "INSERT INTO test VALUES (%s, %s, %s, %s)"
        for _, row in frame.iterrows():
            fmt_sql = format_query(ins, *row)
            tquery(fmt_sql, con=self.conn)

        self.conn.commit()

        result = sql.read_sql("select * from test", con=self.conn)
        result.index = frame.index
        tm.assert_frame_equal(result, frame, rtol=1e-3)

    def test_execute(self):
        frame = tm.makeTimeDataFrame()
        create_sql = sql.get_schema(frame, "test")
        cur = self.conn.cursor()
        cur.execute(create_sql)
        ins = "INSERT INTO test VALUES (?, ?, ?, ?)"

        row = frame.iloc[0]
        sql.execute(ins, self.conn, params=tuple(row))
        self.conn.commit()

        result = sql.read_sql("select * from test", self.conn)
        result.index = frame.index[:1]
        tm.assert_frame_equal(result, frame[:1])

    def test_schema(self):
        frame = tm.makeTimeDataFrame()
        create_sql = sql.get_schema(frame, "test")
        lines = create_sql.splitlines()
        for line in lines:
            tokens = line.split(" ")
            if len(tokens) == 2 and tokens[0] == "A":
                assert tokens[1] == "DATETIME"

        create_sql = sql.get_schema(frame, "test", keys=["A", "B"])
        lines = create_sql.splitlines()
        assert 'PRIMARY KEY ("A", "B")' in create_sql
        cur = self.conn.cursor()
        cur.execute(create_sql)

    def test_execute_fail(self):
        create_sql = """
        CREATE TABLE test
        (
        a TEXT,
        b TEXT,
        c REAL,
        PRIMARY KEY (a, b)
        );
        """
        cur = self.conn.cursor()
        cur.execute(create_sql)

        sql.execute('INSERT INTO test VALUES("foo", "bar", 1.234)', self.conn)
        sql.execute('INSERT INTO test VALUES("foo", "baz", 2.567)', self.conn)

        with pytest.raises(sql.DatabaseError, match="Execution failed on sql"):
            sql.execute('INSERT INTO test VALUES("foo", "bar", 7)', self.conn)

    def test_execute_closed_connection(self):
        create_sql = """
        CREATE TABLE test
        (
        a TEXT,
        b TEXT,
        c REAL,
        PRIMARY KEY (a, b)
        );
        """
        cur = self.conn.cursor()
        cur.execute(create_sql)

        sql.execute('INSERT INTO test VALUES("foo", "bar", 1.234)', self.conn)
        self.conn.close()

        with tm.external_error_raised(sqlite3.ProgrammingError):
            tquery("select * from test", con=self.conn)

    def test_keyword_as_column_names(self):
        df = DataFrame({"From": np.ones(5)})
        sql.to_sql(df, con=self.conn, name="testkeywords", index=False)

    def test_onecolumn_of_integer(self):
        # GH 3628
        # a column_of_integers dataframe should transfer well to sql

        mono_df = DataFrame([1, 2], columns=["c0"])
        sql.to_sql(mono_df, con=self.conn, name="mono_df", index=False)
        # computing the sum via sql
        con_x = self.conn
        the_sum = sum(my_c0[0] for my_c0 in con_x.execute("select * from mono_df"))
        # it should not fail, and gives 3 ( Issue #3628 )
        assert the_sum == 3

        result = sql.read_sql("select * from mono_df", con_x)
        tm.assert_frame_equal(result, mono_df)

    def test_if_exists(self):
        df_if_exists_1 = DataFrame({"col1": [1, 2], "col2": ["A", "B"]})
        df_if_exists_2 = DataFrame({"col1": [3, 4, 5], "col2": ["C", "D", "E"]})
        table_name = "table_if_exists"
        sql_select = f"SELECT * FROM {table_name}"

        msg = "'notvalidvalue' is not valid for if_exists"
        with pytest.raises(ValueError, match=msg):
            sql.to_sql(
                frame=df_if_exists_1,
                con=self.conn,
                name=table_name,
                if_exists="notvalidvalue",
            )
        self.drop_table(table_name)

        # test if_exists='fail'
        sql.to_sql(
            frame=df_if_exists_1, con=self.conn, name=table_name, if_exists="fail"
        )
        msg = "Table 'table_if_exists' already exists"
        with pytest.raises(ValueError, match=msg):
            sql.to_sql(
                frame=df_if_exists_1, con=self.conn, name=table_name, if_exists="fail"
            )
        # test if_exists='replace'
        sql.to_sql(
            frame=df_if_exists_1,
            con=self.conn,
            name=table_name,
            if_exists="replace",
            index=False,
        )
        assert tquery(sql_select, con=self.conn) == [(1, "A"), (2, "B")]
        sql.to_sql(
            frame=df_if_exists_2,
            con=self.conn,
            name=table_name,
            if_exists="replace",
            index=False,
        )
        assert tquery(sql_select, con=self.conn) == [(3, "C"), (4, "D"), (5, "E")]
        self.drop_table(table_name)

        # test if_exists='append'
        sql.to_sql(
            frame=df_if_exists_1,
            con=self.conn,
            name=table_name,
            if_exists="fail",
            index=False,
        )
        assert tquery(sql_select, con=self.conn) == [(1, "A"), (2, "B")]
        sql.to_sql(
            frame=df_if_exists_2,
            con=self.conn,
            name=table_name,
            if_exists="append",
            index=False,
        )
        assert tquery(sql_select, con=self.conn) == [
            (1, "A"),
            (2, "B"),
            (3, "C"),
            (4, "D"),
            (5, "E"),
        ]
        self.drop_table(table_name)
