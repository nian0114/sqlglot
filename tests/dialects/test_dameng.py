from sqlglot import exp, UnsupportedError, ParseError, parse_one
from tests.dialects.test_dialect import Validator
from sqlglot.optimizer.qualify import qualify


class TestDameng(Validator):
    dialect = "dameng"

    def test_dameng(self):
        self.validate_all(
            "SELECT CONNECT_BY_ROOT x AS y",
            write={
                "": "SELECT CONNECT_BY_ROOT x AS y",
                "dameng": "SELECT CONNECT_BY_ROOT x AS y",
            },
        )
        self.parse_one("ALTER TABLE tbl_name DROP FOREIGN KEY fk_symbol").assert_is(exp.Alter)
