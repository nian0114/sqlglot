from __future__ import annotations

import typing as t

from sqlglot import exp, generator, parser, tokens, transforms
from sqlglot.dialects.dialect import (
    Dialect,
    NormalizationStrategy,
    build_timetostr_or_tochar,
    build_formatted_time,
    no_ilike_sql,
    rename_func,
    strposition_sql,
    to_number_with_nls_param,
    trim_sql,
)
from sqlglot.helper import seq_get
from sqlglot.parser import OPTIONS_TYPE, build_coalesce
from sqlglot.tokens import TokenType

if t.TYPE_CHECKING:
    from sqlglot._typing import E


def _trim_sql(self: Dameng.Generator, expression: exp.Trim) -> str:
    position = expression.args.get("position")

    if position and position.upper() in ("LEADING", "TRAILING"):
        return self.trim_sql(expression)

    return trim_sql(self, expression)


def _build_to_timestamp(args: t.List) -> exp.StrToTime | exp.Anonymous:
    if len(args) == 1:
        return exp.Anonymous(this="TO_TIMESTAMP", expressions=args)

    return build_formatted_time(exp.StrToTime, "dameng")(args)


def _increase_to_sql(self: Dameng.Generator, expression: exp.AutoIncrementColumnConstraint) -> str:
    """
    处理自增列：从表属性中查找 AUTO_INCREMENT 的起始值，转换为 IDENTITY(seed, increment)
    """
    seed = 1
    increment = 1

    # 向上查找 CREATE 语句节点
    create_stmt = expression.find_ancestor(exp.Create)

    if create_stmt:
        properties = create_stmt.args.get("properties")
        if properties:
            # 查找 AutoIncrementProperty (例如: AUTO_INCREMENT=1008)
            auto_prop = next(
                (p for p in properties.expressions if isinstance(p, exp.AutoIncrementProperty)),
                None,
            )
            if auto_prop and auto_prop.this:
                try:
                    # 尝试获取值
                    val = auto_prop.this.name
                    if val:
                        seed = int(val)
                except (ValueError, AttributeError):
                    pass

    return f"IDENTITY({seed}, {increment})"


class Dameng(Dialect):
    ALIAS_POST_TABLESAMPLE = True
    LOCKING_READS_SUPPORTED = True
    TABLESAMPLE_SIZE_IS_PERCENT = True
    NULL_ORDERING = "nulls_are_large"
    ON_CONDITION_EMPTY_BEFORE_ERROR = False
    ALTER_TABLE_ADD_REQUIRED_FOR_EACH_COLUMN = False

    # See section 8: https://docs.dameng.com/cd/A97630_01/server.920/a96540/sql_elements9a.htm
    NORMALIZATION_STRATEGY = NormalizationStrategy.UPPERCASE

    # https://docs.dameng.com/database/121/SQLRF/sql_elements004.htm#SQLRF00212
    # https://docs.python.org/3/library/datetime.html#strftime-and-strptime-format-codes
    TIME_MAPPING = {
        "D": "%u",  # Day of week (1-7)
        "DAY": "%A",  # name of day
        "DD": "%d",  # day of month (1-31)
        "DDD": "%j",  # day of year (1-366)
        "DY": "%a",  # abbreviated name of day
        "HH": "%I",  # Hour of day (1-12)
        "HH12": "%I",  # alias for HH
        "HH24": "%H",  # Hour of day (0-23)
        "IW": "%V",  # Calendar week of year (1-52 or 1-53), as defined by the ISO 8601 standard
        "MI": "%M",  # Minute (0-59)
        "MM": "%m",  # Month (01-12; January = 01)
        "MON": "%b",  # Abbreviated name of month
        "MONTH": "%B",  # Name of month
        "SS": "%S",  # Second (0-59)
        "WW": "%W",  # Week of year (1-53)
        "YY": "%y",  # 15
        "YYYY": "%Y",  # 2015
        "FF6": "%f",  # only 6 digits are supported in python formats
    }

    PSEUDOCOLUMNS = {"ROWNUM", "ROWID", "OBJECT_ID", "OBJECT_VALUE", "LEVEL"}

    def quote_identifier(self, expression: E, identify: bool = True) -> E:
        # Disable quoting for pseudocolumns as it may break queries e.g
        # `WHERE "ROWNUM" = ...` does not work but `WHERE ROWNUM = ...` does
        if isinstance(expression, exp.Identifier) and isinstance(
            expression.parent, exp.Pseudocolumn
        ):
            return expression

        return super().quote_identifier(expression, identify=identify)

    class Tokenizer(tokens.Tokenizer):
        VAR_SINGLE_TOKENS = {"@", "$", "#"}

        UNICODE_STRINGS = [
            (prefix + q, q)
            for q in t.cast(t.List[str], tokens.Tokenizer.QUOTES)
            for prefix in ("U", "u")
        ]

        NESTED_COMMENTS = False

        KEYWORDS = {
            **tokens.Tokenizer.KEYWORDS,
            "(+)": TokenType.JOIN_MARKER,
            "BINARY_DOUBLE": TokenType.DOUBLE,
            "BINARY_FLOAT": TokenType.FLOAT,
            "BULK COLLECT INTO": TokenType.BULK_COLLECT_INTO,
            "COLUMNS": TokenType.COLUMN,
            "MATCH_RECOGNIZE": TokenType.MATCH_RECOGNIZE,
            "MINUS": TokenType.EXCEPT,
            "ORDER SIBLINGS BY": TokenType.ORDER_SIBLINGS_BY,
            "SAMPLE": TokenType.TABLE_SAMPLE,
            "START": TokenType.BEGIN,
            "TOP": TokenType.TOP,
        }

    class Parser(parser.Parser):
        WINDOW_BEFORE_PAREN_TOKENS = {TokenType.OVER, TokenType.KEEP}
        VALUES_FOLLOWED_BY_PAREN = False

        FUNCTIONS = {
            **parser.Parser.FUNCTIONS,
            "CONVERT": exp.ConvertToCharset.from_arg_list,
            "L2_DISTANCE": exp.EuclideanDistance.from_arg_list,
            "NVL": lambda args: build_coalesce(args, is_nvl=True),
            "SQUARE": lambda args: exp.Pow(this=seq_get(args, 0), expression=exp.Literal.number(2)),
            "TO_CHAR": build_timetostr_or_tochar,
            "TO_TIMESTAMP": _build_to_timestamp,
            "TO_DATE": build_formatted_time(exp.StrToDate, "dameng"),
            "TRUNC": lambda args: exp.DateTrunc(
                unit=seq_get(args, 1) or exp.Literal.string("DD"),
                this=seq_get(args, 0),
                unabbreviate=False,
            ),
        }

        NO_PAREN_FUNCTION_PARSERS = {
            **parser.Parser.NO_PAREN_FUNCTION_PARSERS,
            "NEXT": lambda self: self._parse_next_value_for(),
            "PRIOR": lambda self: self.expression(exp.Prior, this=self._parse_bitwise()),
            "SYSDATE": lambda self: self.expression(exp.CurrentTimestamp, sysdate=True),
            "DBMS_RANDOM": lambda self: self._parse_dbms_random(),
        }

        FUNCTION_PARSERS: t.Dict[str, t.Callable] = {
            **parser.Parser.FUNCTION_PARSERS,
            "JSON_ARRAY": lambda self: self._parse_json_array(
                exp.JSONArray,
                expressions=self._parse_csv(lambda: self._parse_format_json(self._parse_bitwise())),
            ),
            "JSON_ARRAYAGG": lambda self: self._parse_json_array(
                exp.JSONArrayAgg,
                this=self._parse_format_json(self._parse_bitwise()),
                order=self._parse_order(),
            ),
            "JSON_EXISTS": lambda self: self._parse_json_exists(),
        }
        FUNCTION_PARSERS.pop("CONVERT")

        PROPERTY_PARSERS = {
            **parser.Parser.PROPERTY_PARSERS,
            "GLOBAL": lambda self: self._match_text_seq("TEMPORARY")
            and self.expression(exp.TemporaryProperty, this="GLOBAL"),
            "PRIVATE": lambda self: self._match_text_seq("TEMPORARY")
            and self.expression(exp.TemporaryProperty, this="PRIVATE"),
            "FORCE": lambda self: self.expression(exp.ForceProperty),
        }

        QUERY_MODIFIER_PARSERS = {
            **parser.Parser.QUERY_MODIFIER_PARSERS,
            TokenType.ORDER_SIBLINGS_BY: lambda self: ("order", self._parse_order()),
            TokenType.WITH: lambda self: ("options", [self._parse_query_restrictions()]),
        }

        TYPE_LITERAL_PARSERS = {
            exp.DataType.Type.DATE: lambda self, this, _: self.expression(
                exp.DateStrToDate, this=this
            )
        }

        # SELECT UNIQUE .. is old-style Dameng syntax for SELECT DISTINCT ..
        # Reference: https://stackoverflow.com/a/336455
        DISTINCT_TOKENS = {TokenType.DISTINCT, TokenType.UNIQUE}

        QUERY_RESTRICTIONS: OPTIONS_TYPE = {
            "WITH": (
                ("READ", "ONLY"),
                ("CHECK", "OPTION"),
            ),
        }

        def _parse_dbms_random(self) -> t.Optional[exp.Expression]:
            if self._match_text_seq(".", "VALUE"):
                lower, upper = None, None
                if self._match(TokenType.L_PAREN, advance=False):
                    lower_upper = self._parse_wrapped_csv(self._parse_bitwise)
                    if len(lower_upper) == 2:
                        lower, upper = lower_upper

                return exp.Rand(lower=lower, upper=upper)

            self._retreat(self._index - 1)
            return None

        def _parse_json_array(self, expr_type: t.Type[E], **kwargs) -> E:
            return self.expression(
                expr_type,
                null_handling=self._parse_on_handling("NULL", "NULL", "ABSENT"),
                return_type=self._match_text_seq("RETURNING") and self._parse_type(),
                strict=self._match_text_seq("STRICT"),
                **kwargs,
            )

        def _parse_hint_function_call(self) -> t.Optional[exp.Expression]:
            if not self._curr or not self._next or self._next.token_type != TokenType.L_PAREN:
                return None

            this = self._curr.text

            self._advance(2)
            args = self._parse_hint_args()
            this = self.expression(exp.Anonymous, this=this, expressions=args)
            self._match_r_paren(this)
            return this

        def _parse_hint_args(self):
            args = []
            result = self._parse_var()

            while result:
                args.append(result)
                result = self._parse_var()

            return args

        def _parse_query_restrictions(self) -> t.Optional[exp.Expression]:
            kind = self._parse_var_from_options(self.QUERY_RESTRICTIONS, raise_unmatched=False)

            if not kind:
                return None

            return self.expression(
                exp.QueryOption,
                this=kind,
                expression=self._match(TokenType.CONSTRAINT) and self._parse_field(),
            )

        def _parse_json_exists(self) -> exp.JSONExists:
            this = self._parse_format_json(self._parse_bitwise())
            self._match(TokenType.COMMA)
            return self.expression(
                exp.JSONExists,
                this=this,
                path=self.dialect.to_json_path(self._parse_bitwise()),
                passing=self._match_text_seq("PASSING")
                and self._parse_csv(lambda: self._parse_alias(self._parse_bitwise())),
                on_condition=self._parse_on_condition(),
            )

        def _parse_into(self) -> t.Optional[exp.Into]:
            # https://docs.dameng.com/en/database/dameng/dameng-database/19/lnpls/SELECT-INTO-statement.html
            bulk_collect = self._match(TokenType.BULK_COLLECT_INTO)
            if not bulk_collect and not self._match(TokenType.INTO):
                return None

            index = self._index

            expressions = self._parse_expressions()
            if len(expressions) == 1:
                self._retreat(index)
                self._match(TokenType.TABLE)
                return self.expression(
                    exp.Into, this=self._parse_table(schema=True), bulk_collect=bulk_collect
                )

            return self.expression(exp.Into, bulk_collect=bulk_collect, expressions=expressions)

        def _parse_connect_with_prior(self):
            return self._parse_assignment()

        def _parse_insert_table(self) -> t.Optional[exp.Expression]:
            # Dameng does not use AS for INSERT INTO alias
            # https://docs.dameng.com/en/database/dameng/dameng-database/18/sqlrf/INSERT.html
            # Parse table parts without schema to avoid parsing the alias with its columns
            this = self._parse_table_parts(schema=True)

            if isinstance(this, exp.Table):
                alias_name = self._parse_id_var(any_token=False)
                if alias_name:
                    this.set("alias", exp.TableAlias(this=alias_name))

                this.set("partition", self._parse_partition())

                # Now parse the schema (column list) if present
                return self._parse_schema(this=this)

            return this

    class Generator(generator.Generator):
        LOCKING_READS_SUPPORTED = True
        JOIN_HINTS = False
        TABLE_HINTS = False
        DATA_TYPE_SPECIFIERS_ALLOWED = True
        ALTER_TABLE_INCLUDE_COLUMN_KEYWORD = False
        LIMIT_FETCH = "FETCH"
        TABLESAMPLE_KEYWORDS = "SAMPLE"
        LAST_DAY_SUPPORTS_DATE_PART = False
        SUPPORTS_SELECT_INTO = True
        TZ_TO_WITH_TIME_ZONE = True
        SUPPORTS_WINDOW_EXCLUDE = True
        QUERY_HINT_SEP = " "
        SUPPORTS_DECODE_CASE = True

        TYPE_MAPPING = {
            **generator.Generator.TYPE_MAPPING,
            exp.DataType.Type.UTINYINT: "TINYINT",
            exp.DataType.Type.USMALLINT: "SMALLINT",
            exp.DataType.Type.UMEDIUMINT: "INT",
            exp.DataType.Type.UINT: "INT",
            exp.DataType.Type.UBIGINT: "BIGINT",
            exp.DataType.Type.MEDIUMINT: "INT",
            exp.DataType.Type.DECIMAL: "NUMBER",
            exp.DataType.Type.DOUBLE: "DOUBLE PRECISION",
            exp.DataType.Type.VARCHAR: "VARCHAR",
            exp.DataType.Type.NCHAR: "NCHAR",
            exp.DataType.Type.TEXT: "CLOB",
            exp.DataType.Type.LONGTEXT: "CLOB",
            exp.DataType.Type.TIMETZ: "TIME",
            exp.DataType.Type.TIMESTAMPNTZ: "TIMESTAMP",
            exp.DataType.Type.TIMESTAMPTZ: "TIMESTAMP",
            exp.DataType.Type.BINARY: "BLOB",
            exp.DataType.Type.VARBINARY: "BLOB",
            exp.DataType.Type.ROWVERSION: "BLOB",
            exp.DataType.Type.DATETIME: "TIMESTAMP",
        }

        def datatype_sql(self, expression: exp.DataType) -> str:
            """
            重写数据类型生成逻辑。
            这是 sqlglot 处理数据类型的核心入口。
            """
            # https://eco.dameng.com/document/dm/zh-cn/pm/dm_sql-introduction
            # target_types = ("TINYINT", "INT", "BIGINT", "INTEGER", "PLS_INTEGER", "BYTE", "SMALLINT")
            NO_PARAM_TYPES = {
                # 有符号
                exp.DataType.Type.TINYINT,
                exp.DataType.Type.SMALLINT,
                exp.DataType.Type.MEDIUMINT,
                exp.DataType.Type.INT,
                exp.DataType.Type.BIGINT,
                # 无符号 (Unsigned)
                exp.DataType.Type.UTINYINT,
                exp.DataType.Type.USMALLINT,
                exp.DataType.Type.UMEDIUMINT,
                exp.DataType.Type.UINT,
                exp.DataType.Type.UBIGINT,
            }

            if expression.this in NO_PARAM_TYPES:
                # expression.this 直接就是 exp.DataType.Type 枚举对象 (例如 exp.DataType.Type.INT)
                # 尝试从 TYPE_MAPPING 获取，如果没有，默认使用类型本身的字符串值
                return self.TYPE_MAPPING.get(expression.this) or expression.this.value
            # 2. 其他类型 (如 VARCHAR(32)) 仍然走父类的标准逻辑
            return super().datatype_sql(expression)

        TRANSFORMS = {
            **generator.Generator.TRANSFORMS,
            # --- Dameng 特殊处理开始 ---
            # 1. 自增列转换为 IDENTITY
            exp.AutoIncrementColumnConstraint: _increase_to_sql,
            # 2. 过滤掉 MySQL 特有的索引参数 (如 USING BTREE)
            exp.IndexParameters: lambda self, e: "",
            # 3. 过滤掉列级字符集定义 (达梦使用库级或默认)
            exp.CharacterSetColumnConstraint: lambda self, e: "",
            exp.CollateColumnConstraint: lambda self, e: "",
            # 4. 隐藏行内注释 (将在 create_sql 中单独生成)
            exp.CommentColumnConstraint: lambda self, e: "",
            exp.IndexColumnConstraint: lambda self, e: "",
            # 5. 【新增】过滤 ON UPDATE CURRENT_TIMESTAMP
            # 达梦不支持列定义中的自动更新时间，需要触发器实现。这里先忽略以防报错。
            exp.OnUpdateColumnConstraint: lambda self, e: "",
            # 5. 过滤掉表属性 (ENGINE, ROW_FORMAT 等由 PROPERTIES_LOCATION 处理，但为了保险 transform 也设为空)
            exp.EngineProperty: lambda self, e: "",
            exp.RowFormatProperty: lambda self, e: "",
            # --- Dameng 特殊处理结束 ---
            exp.DateStrToDate: lambda self, e: self.func(
                "TO_DATE", e.this, exp.Literal.string("YYYY-MM-DD")
            ),
            exp.DateTrunc: lambda self, e: self.func("TRUNC", e.this, e.unit),
            exp.EuclideanDistance: rename_func("L2_DISTANCE"),
            exp.ILike: no_ilike_sql,
            exp.LogicalOr: rename_func("MAX"),
            exp.LogicalAnd: rename_func("MIN"),
            exp.Mod: rename_func("MOD"),
            exp.Rand: rename_func("DBMS_RANDOM.VALUE"),
            exp.Select: transforms.preprocess(
                [
                    transforms.eliminate_distinct_on,
                    transforms.eliminate_qualify,
                ]
            ),
            exp.StrPosition: lambda self, e: (
                strposition_sql(
                    self, e, func_name="INSTR", supports_position=True, supports_occurrence=True
                )
            ),
            exp.StrToTime: lambda self, e: self.func("TO_TIMESTAMP", e.this, self.format_time(e)),
            exp.StrToDate: lambda self, e: self.func("TO_DATE", e.this, self.format_time(e)),
            exp.Subquery: lambda self, e: self.subquery_sql(e, sep=" "),
            exp.Substring: rename_func("SUBSTR"),
            exp.Table: lambda self, e: self.table_sql(e, sep=" "),
            exp.TableSample: lambda self, e: self.tablesample_sql(e),
            exp.TemporaryProperty: lambda _, e: f"{e.name or 'GLOBAL'} TEMPORARY",
            exp.TimeToStr: lambda self, e: self.func("TO_CHAR", e.this, self.format_time(e)),
            exp.ToChar: lambda self, e: self.function_fallback_sql(e),
            exp.ToNumber: to_number_with_nls_param,
            exp.Trim: _trim_sql,
            exp.Unicode: lambda self, e: f"ASCII(UNISTR({self.sql(e.this)}))",
            exp.UnixToTime: lambda self,
            e: f"TO_DATE('1970-01-01', 'YYYY-MM-DD') + ({self.sql(e, 'this')} / 86400)",
            exp.UtcTimestamp: rename_func("UTC_TIMESTAMP"),
            exp.UtcTime: rename_func("UTC_TIME"),
        }

        PROPERTIES_LOCATION = {
            **generator.Generator.PROPERTIES_LOCATION,
            exp.VolatileProperty: exp.Properties.Location.UNSUPPORTED,
            # 将以下属性标记为不支持，防止生成在 CREATE TABLE 末尾
            exp.AutoIncrementProperty: exp.Properties.Location.UNSUPPORTED,
            exp.EngineProperty: exp.Properties.Location.UNSUPPORTED,
            exp.CharacterSetProperty: exp.Properties.Location.UNSUPPORTED,
            exp.RowFormatProperty: exp.Properties.Location.UNSUPPORTED,
            exp.CollateProperty: exp.Properties.Location.UNSUPPORTED,
        }

        def constraint_sql(self, expression: exp.Constraint) -> str:
            constraint_kind = expression.expressions[0] if expression.expressions else None
            if isinstance(constraint_kind, exp.ForeignKey):
                # 获取 Reference 节点 (包含引用表、列和选项)
                reference = constraint_kind.args.get("reference")

                if reference:
                    # 【核心修改】直接清空 options 列表
                    # 这样 super().constraint_sql() 就不会生成 ON DELETE/UPDATE 了
                    reference.set("options", [])

                # 生成基础 SQL (例如: CONSTRAINT "name" FOREIGN KEY(...) REFERENCES "table"("id"))
                sql = super().constraint_sql(expression)

                # 在末尾追加达梦需要的 WITH INDEX
                return f"{sql} WITH INDEX"

            return super().constraint_sql(expression)

        def uniquecolumnconstraint_sql(self, expression: exp.UniqueColumnConstraint) -> str:
            schema = expression.this
            if isinstance(schema, exp.Schema):
                constraint_name = self.sql(schema, "this")
                columns = self.expressions(schema, flat=True)
                return f"CONSTRAINT {constraint_name} UNIQUE({columns})"
            return super().uniquecolumnconstraint_sql(expression)

        def create_sql(self, expression: exp.Create) -> str:
            """
            重写 create_sql：
            1. 调用父类生成标准的 Create Table 语句 (注释和不支持的属性已被 transforms 过滤)
            2. 遍历 AST 提取列注释
            3. 将注释拼接在 Create 语句后面返回
            """
            # 1. 生成主 SQL
            main_sql = super().create_sql(expression)

            # 2. 提取并生成注释 SQL
            comment_sqls = []

            # 确保这是建表语句并且包含列定义
            if isinstance(expression.this, exp.Schema) and isinstance(
                expression.this.this, exp.Table
            ):
                table_node = expression.this.this

                # 遍历所有列
                for col_def in expression.this.expressions:
                    if isinstance(col_def, exp.ColumnDef):
                        constraints = col_def.args.get("constraints", [])
                        # 查找是否有 Comment 约束
                        comment_constraint = next(
                            (
                                c
                                for c in constraints
                                if isinstance(c.kind, exp.CommentColumnConstraint)
                            ),
                            None,
                        )

                        if comment_constraint:
                            # 提取注释文本
                            comment_text = comment_constraint.kind.this.name

                            # 构建列对象: "schema"."table"."column"
                            # 必须手动构建，确保引用正确
                            col_ref = exp.Column(
                                this=col_def.this,
                                table=table_node.this,
                                db=table_node.args.get("db"),
                                quoted=True,  # 强制引用
                            )

                            # 生成 COMMENT ON COLUMN ... 语句
                            comment_stmt = exp.Comment(
                                this=col_ref,
                                kind="COLUMN",
                                expression=exp.Literal.string(comment_text),
                            )

                            # 使用当前 generator 生成这条 SQL
                            comment_sqls.append(self.sql(comment_stmt))

            # 3. 合并返回
            if comment_sqls:
                # 达梦支持多条语句以 ; 分隔
                return f"{main_sql};\n" + ";\n".join(comment_sqls)
            print(main_sql)

            return main_sql

        def currenttimestamp_sql(self, expression: exp.CurrentTimestamp) -> str:
            if expression.args.get("sysdate"):
                return "SYSDATE"

            this = expression.this
            return self.func("CURRENT_TIMESTAMP", this) if this else "CURRENT_TIMESTAMP"

        def offset_sql(self, expression: exp.Offset) -> str:
            return f"{super().offset_sql(expression)} ROWS"

        def add_column_sql(self, expression: exp.Expression) -> str:
            return f"ADD {self.sql(expression)}"

        def queryoption_sql(self, expression: exp.QueryOption) -> str:
            option = self.sql(expression, "this")
            value = self.sql(expression, "expression")
            value = f" CONSTRAINT {value}" if value else ""

            return f"{option}{value}"

        def coalesce_sql(self, expression: exp.Coalesce) -> str:
            func_name = "NVL" if expression.args.get("is_nvl") else "COALESCE"
            return rename_func(func_name)(self, expression)

        def into_sql(self, expression: exp.Into) -> str:
            into = "INTO" if not expression.args.get("bulk_collect") else "BULK COLLECT INTO"
            if expression.this:
                return f"{self.seg(into)} {self.sql(expression, 'this')}"

            return f"{self.seg(into)} {self.expressions(expression)}"

        def hint_sql(self, expression: exp.Hint) -> str:
            expressions = []

            for expression in expression.expressions:
                if isinstance(expression, exp.Anonymous):
                    formatted_args = self.format_args(*expression.expressions, sep=" ")
                    expressions.append(f"{self.sql(expression, 'this')}({formatted_args})")
                else:
                    expressions.append(self.sql(expression))

            return f" /*+ {self.expressions(sqls=expressions, sep=self.QUERY_HINT_SEP).strip()} */"

        def isascii_sql(self, expression: exp.IsAscii) -> str:
            return f"NVL(REGEXP_LIKE({self.sql(expression.this)}, '^[' || CHR(1) || '-' || CHR(127) || ']*$'), TRUE)"
