"""
Contains logic to aggregate using Postgres / Django ORM, similar to amcates.py.
"""
import uuid
import collections
import itertools

from django.db.models import Avg as DAvg, Count as DCount
from django.db import connection

from amcat.models import Article, Medium
from amcat.models import Code, Coding, CodingValue, CodedArticle, CodingSchemaField

from amcat.models.coding.codingschemafield import  FIELDTYPE_IDS

POSTGRES_DATE_TRUNC_VALUES = [
    "microseconds",
    "milliseconds",
    "second",
    "minute",
    "hour",
    "day",
    "week",
    "month",
    "quarter",
    "year",
    "decade",
    "century",
    "millennium"
]

INNER_JOIN = r'INNER JOIN "{table}" AS "T{{prefix}}_{table}" ON ("{prefix}{t1}"."{f1}" = "T{{prefix}}_{table}"."{f2}")'

class JOINS:
    codings = INNER_JOIN.format(
        table=Coding._meta.db_table, 
        t1=CodingValue._meta.db_table, 
        f1="coding_id", 
        f2="coding_id",
        prefix=""
    )

    coded_articles = INNER_JOIN.format(
        table=CodedArticle._meta.db_table,
        t1=Coding._meta.db_table, 
        f1="coded_article_id",
        f2="id",
        prefix="T_"
    )
    
    articles = INNER_JOIN.format(
        table=Article._meta.db_table, 
        t1=CodedArticle._meta.db_table,
        f1="article_id", 
        f2="article_id",
        prefix="T_"
    )

    codings_values = INNER_JOIN.format(
        table=CodingValue._meta.db_table,
        t1=Coding._meta.db_table, 
        f1="coding_id", 
        f2="coding_id",
        prefix="T_"
    )

DEFAULT_JOINS = (
    JOINS.codings.format(prefix=""),
    JOINS.coded_articles.format(prefix=""),
    JOINS.articles.format(prefix="")
)

class SQLObject(object):
    def get_select(self):
        raise NotImplementedError("Subclasses should implement get_select().")

    def get_joins(self):
        return ()

    def get_wheres(self):
        return ()

    def get_group_by(self):
        return None

class Category(SQLObject):
    model = None
    
    def get_group_by(self):
        return self.get_select()

class IntervalCategory(Category):
    DATE_TRUNC_SQL = 'date_trunc(\'{interval}\', "T_articles"."date")'

    def __init__(self, interval):
        super(IntervalCategory, self).__init__()

        if interval not in POSTGRES_DATE_TRUNC_VALUES:
            err_msg = "{} not a valid interval. Choose on of: {}"
            raise ValueError("".format(interval, POSTGRES_DATE_TRUNC_VALUES))

        self.interval = interval

    def get_select(self):
        return DATE_TRUNC_SQL.format(interval=self.interval)

    def __repr__(self):
        return "<Interval: %s>" % self.interval

class MediumCategory(Category):
    model = Medium

    def get_select(self):
        return '"T_articles"."medium_id"'

class SchemafieldCategory(Category):
    model = Code

    def __init__(self, field, *args, **kwargs):
        super(SchemafieldCategory, self).__init__(*args, **kwargs)
        self.field = field

    def get_select(self):
        return '"codings_values"."intval"'

    def get_wheres(self):
        where_sql = '"codings_values"."field_id" = {field.id}'
        yield where_sql.format(field=self.field)

    def __repr__(self):
        return "<SchemafieldCategory: %s>" % self.field

class BaseAggregationValue(SQLObject):
    def __init__(self, prefix=None):
        super(BaseAggregationValue, self).__init__()
        self.prefix = uuid.uuid4().hex if prefix is None else prefix

class Average(BaseAggregationValue):
    def __init__(self, field):
        """@type field: CodingSchemaField"""
        super(Average, self).__init__()
        assert_msg = "Average only aggregates on codingschemafields for now"
        assert isinstance(field, CodingSchemaField), assert_msg
        self.field = field

    def get_joins(self):
        yield JOINS.codings_values.format(prefix=self.prefix)

    def get_wheres(self):
        where_sql = '"T{prefix}_codings_values"."field_id" = {field_id}'
        yield where_sql.format(field_id=self.field.id, prefix=self.prefix)

    def get_select(self):
        return 'AVG("T{prefix}_codings_values"."intval")'.format(prefix=self.prefix)

    def __repr__(self):
        return "<Average: %s>" % self.field

class Count(BaseAggregationValue):
    def get_select(self):
        return 'COUNT(DISTINCT("T_articles"."article_id"))'

class ORMAggregate(object):
    def __init__(self, codings, flat=False):
        """
        @type codings: QuerySet
        """
        self.codings = codings
        self.flat = flat

    @classmethod
    def from_articles(self, article_ids, codingjob_ids, **kwargs):
        """
        @type article_ids: sequence of ints
        @type codingjob_ids: sequence of ints
        """
        codings = Coding.objects.filter(coded_article__article__id__in=article_ids)
        codings = codings.filter(coded_article__codingjob__id__in=codingjob_ids)
        return ORMAggregate(codings, **kwargs)

    def _get_aggregate_sql(self, categories, values):
        sql = 'SELECT {selects} FROM "codings_values" {joins} WHERE {wheres}'
        if categories:
            sql += " GROUP BY {groups}"
        sql += ';'

        codings_ids = tuple(self.codings.values_list("id", flat=True))
        wheres = ['"codings_values"."coding_id" IN {}'.format(codings_ids)]

        # Gather all separate sql statements
        selects, joins, groups = [], list(DEFAULT_JOINS), []
        for sqlobj in itertools.chain(categories, values):
            selects.append(sqlobj.get_select())
            groups.append(sqlobj.get_group_by())

            for join in sqlobj.get_joins():
                joins.append(join)
            
            for where in sqlobj.get_wheres():
                wheres.append(where)

        # Build sql statement
        return sql.format(
            selects=",".join(filter(None, selects)),
            joins=" ".join(filter(None, joins)),
            wheres="({})".format(") AND (".join(filter(None, wheres))),
            groups=",".join(filter(None, groups))
        )

    def _get_aggregate(self, categories, values):
        sql = self._get_aggregate_sql(categories, values)

        # Execute sql
        aggregation = []
        with connection.cursor() as c:
            c.execute(sql)
            aggregation = list(map(list, c.fetchall()))

        # Replace ids with model objects
        for i, category in enumerate(categories):
            if category.model is None:
                continue
            pks = [row[i] for row in aggregation]
            objs = category.model.objects.in_bulk(pks)
            for row in aggregation:
                row[i] = objs[row[i]]

        # Flat representation to ([cats], [vals])
        num_categories = len(categories)
        for row in aggregation:
            yield tuple(row[:num_categories]), tuple(row[num_categories:])

    def get_aggregate(self, categories=(), values=()):
        """
        @type categories: iterable of Category
        @type values: iterable of Value
        """
        if not values:
            raise ValueError("You must specify at least one value.")

        aggregation = self._get_aggregate(list(categories), list(values))

        # Flatten categories, i.e. [((Medium,), (1, 2))] to [((Medium, (1, 2))]
        if self.flat and len(categories) == 1:
            aggregation = ((cat[0], vals) for cat, vals in aggregation)

        # Flatten values, i.e. [(Medium, (1,))] to [(Medium, 1)]
        if self.flat and len(values) == 1:
            aggregation = ((cats, val[0]) for cats, val in aggregation)

        return aggregation

