###########################################################################
#          (C) Vrije Universiteit, Amsterdam (the Netherlands)            #
#                                                                         #
# This file is part of AmCAT - The Amsterdam Content Analysis Toolkit     #
#                                                                         #
# AmCAT is free software: you can redistribute it and/or modify it under  #
# the terms of the GNU Affero General Public License as published by the  #
# Free Software Foundation, either version 3 of the License, or (at your  #
# option) any later version.                                              #
#                                                                         #
# AmCAT is distributed in the hope that it will be useful, but WITHOUT    #
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or   #
# FITNESS FOR A PARTICULAR PURPOSE. See the GNU Affero General Public     #
# License for more details.                                               #
#                                                                         #
# You should have received a copy of the GNU Affero General Public        #
# License along with AmCAT.  If not, see <http://www.gnu.org/licenses/>.  #
###########################################################################

from __future__ import unicode_literals, print_function, absolute_import

import re
import itertools

from django.http import QueryDict

from rest_framework.fields import DateField, CharField, IntegerField
from rest_framework.serializers import Serializer
from django_filters import filters, filterset

from amcat.tools import amcates, keywordsearch
from api.rest.resources.amcatresource import AmCATResource
from amcat.tools.caching import cached
from amcat.models import Project, authorisation, Article


# NOTE: Adding 'page' to filter fields introduces ambiguity (article-page vs. API page)
FILTER_FIELDS = frozenset({"start_date", "end_date", "mediumid", "sets", "section"})
FILTER_SINGLE_FIELDS = frozenset({"start_date", "end_date", "section"})
FILTER_ID_FIELDS = frozenset({"ids", "pk"})

RE_KWIC = re.compile("(?P<left>.*?)<em>(?P<keyword>.*?)</em>(?P<right>.*)", re.DOTALL)


class LazyES(object):
    def __init__(self, user=None, queries=None, filters=None, fields=None, hits=False):
        self.user = user
        self.queries = queries
        self.filters = filters or {}
        self.fields = [f for f in (fields or []) if f != "id"]
        self.es = amcates.ES()
        self.hits = hits
        self._count = None

    @property
    def query(self):
        if self.queries:
            return "\n".join("({q.query})".format(**locals()) for q in self.queries)

    def filter(self, key, value):
        self.filters[key] = value

    def __len__(self):
        if not self._count:
            self._count = self.es.count(self.query, filters=self.filters)
        return self._count

    def __getslice__(self, i, j):
        query_kargs = {}

        if self.query and ("lead" in self.fields or "headline" in self.fields):
            query_kargs["highlight"] = True
        elif "lead" in self.fields:
            query_kargs["lead"] = True

        fields = [f for f in self.fields if f != "lead"]

        if "text" in fields and "projectid" not in fields:
            fields.append("projectid")

        result = self.es.query(
            query=self.query,
            filters=self.filters,
            fields=fields,
            size=j - i,
            sort=["id"],
            from_=i,
            score=False,
            **query_kargs
        )

        if self.hits:
            def add_hits_column(r):
                r.hits = {q.label : 0 for q in self.queries}
                return r

            result_dict = {r.id : add_hits_column(r) for r in result}
            filters = {'ids': list(result_dict)}

            for q in self.queries:
                for hit in self.es.query_all(q.query, filters=filters, fields=[]):
                    result_dict[hit.id].hits[q.label] = hit.score

        cache = {} # projectid -> is_reader
        def _check_text_permission(user, row):
            if user is None:
                is_reader = False
            else:
                try:
                    is_reader = cache[row.projectid]
                except KeyError:
                    is_reader = user.userprofile.has_role(
                        authorisation.ROLE_PROJECT_READER,
                        Project.objects.get(pk=row.projectid)
                    )
                    cache[row.projectid] = is_reader
            if not is_reader:
                row.text = None
            return row

        if "text" in fields:
            result = [_check_text_permission(self.user, row) for row in result]

        return result


class HighlightField(CharField):
    def field_to_native(self, obj, field_name):
        # use highlighting if available, otherwise fall back to raw text
        source = self.source or field_name
        target = {'lead' : 'text', 'headline' : 'headline'}[source]
        result = getattr(obj, "highlight", {}).get(target)
        if result:
            return " ... ".join(result)
        else:
            return getattr(obj, source, None)


class KWICField(CharField):
    def __init__(self, *args, **kargs):
        self.kwic = kargs.pop('kwic')
        super(KWICField, self).__init__(*args, **kargs)

    def field_to_native(self, obj, field_name):
        # use highlighting if available, otherwise fall back to raw text
        if obj is None:
            return None

        hl = obj.highlight.get('headline')
        if not hl:
            hl = obj.highlight.get('text')

        if hl:
            # try to get match of first word
            use_hl = hl[0]
            if obj._searchresult.query:
                matches = [RE_KWIC.match(x) for x in hl]
                matches = [x.groupdict()['keyword'].lower() for x in matches if x]
                query = re.sub("[^\w ]", "", obj._searchresult.query)
                query = query.split()[0].lower()
                if query in matches:
                    use_hl = hl[matches.index(query)]
                else:
                    for i, match in enumerate(matches):
                        if match.startswith(query):
                            use_hl = hl[i]
                            break

            m = RE_KWIC.match(use_hl)
            if m:
                val = m.groupdict()[self.kwic]
                val = re.sub('<[^<]+?>', '', val)
                return val


class ScoreField(IntegerField):
    def field_to_native(self, obj, field_name):
        source = self.source or field_name
        return obj and obj.hits.get(source, 0)


class SearchResourceSerialiser(Serializer):
    id = IntegerField()
    date = DateField()
    headline = HighlightField()
    mediumid = IntegerField()
    medium = CharField()
    creator = CharField()
    byline = CharField()
    addressee = CharField()
    section = CharField()
    url = CharField()
    length = IntegerField()
    page = IntegerField()

    def __init__(self, *args, **kwargs):
        Serializer.__init__(self, *args, **kwargs)
        ctx = kwargs.get("context", {})
        columns = ctx.get("columns", [])
        queries = ctx.get("queries", [])

        if ctx.get('minimal'):
            for fn in list(self.fields):
                if fn != 'id' and fn not in list(columns):
                    del self.fields[fn]

        if "hits" in columns and queries:
            for q in queries:
                self.fields[q.label] = ScoreField()
        if "text" in columns:
            self.fields['text'] = CharField()
        if "projectid" in columns:
            self.fields['projectid'] = IntegerField()
        if "lead" in columns:
            self.fields['lead'] = HighlightField()
        if "kwic" in columns:
            self.fields['left'] = KWICField(kwic='left')
            self.fields['keyword'] = KWICField(kwic='keyword')
            self.fields['right'] = KWICField(kwic='right')


class SearchResource(AmCATResource):
    model = Article
    serializer_class = SearchResourceSerialiser

    def post(self, request, *args, **kwargs):
        # allow for POST requests
        return self.list(request, *args, **kwargs)

    @property
    @cached
    def params(self):
        q = QueryDict("", mutable=True)
        q.update(self.request.QUERY_PARAMS)
        q.update(self.request.DATA)
        return QueryDict(q.urlencode())


    @property
    @cached
    def columns(self):
        return self.params.getlist("col")

    @property
    @cached
    def queries(self):
        queries = filter(bool, self.params.getlist("q"))
        return map(keywordsearch.SearchQuery.from_string, queries)

    def get_queryset(self):
        fields = self.get_serializer().get_fields().keys()
        if "text" in self.columns: fields += ["text"]
        if "lead" in self.columns: fields += ["lead"]
        if "kwic" in self.columns and "lead" not in fields: fields += ["lead"]
        hits = "hits" in self.columns

        print(self.queries)
        print(fields)
        return LazyES(self.request.user, self.queries, fields=fields, hits=hits)

    def filter_queryset(self, queryset):
        # Allow for both 'ids' and 'pk' filtering
        ids = [self.params.getlist(field_name) for field_name in FILTER_ID_FIELDS]
        ids = list(itertools.chain.from_iterable(ids))
        if ids:
            queryset.filter("ids", ids)

        for k in FILTER_FIELDS:
            if k in self.params:
                if k in FILTER_SINGLE_FIELDS:
                    queryset.filter(k, self.params.get(k))
                else:
                    queryset.filter(k, self.params.getlist(k))

        return queryset

    @classmethod
    def get_model_name(cls):
        return "search"

    def get_filter_fields(cls):
        return cls.filter_class().filters.keys()

    class filter_class(filterset.FilterSet):
        sets = filters.NumberFilter()
        def __init__(self, data=None, queryset=None, prefix=None):
            if queryset is None:
                queryset = LazyES()
            filterset.FilterSet.__init__(self, data, queryset, prefix)

        class Meta:
            order_by = True

    def get_serializer_context(self):
        ctx = super(SearchResource, self).get_serializer_context()
        ctx["queries"] = self.queries
        ctx["columns"] = self.columns
        ctx["minimal"] = self.params.get('minimal')
        return ctx

    @classmethod
    def _extra_fields(cls, cols, queries):
        if 'hits' in cols:
            for q in queries:
                q = keywordsearch.SearchQuery.from_string(q)
                if q.query != "*":
                    yield q.label
        if 'text' in cols:
            yield 'text'
        if 'lead' in cols:
            yield 'lead'
        if 'projectid' in cols:
            yield 'projectid'
        if 'kwic' in cols:
            yield 'left'
            yield 'keyword'
            yield 'right'

    @classmethod
    def extra_fields(cls, args):
        """Used by datatable.py for dynamic fields. Hack?"""
        print(args)
        queries = [val for (name, val) in args if name == "q"]
        cols = [val for (name, val) in args if name == "col"]
        return list(cls._extra_fields(cols, queries))


###########################################################################
#                          U N I T   T E S T S                            #
###########################################################################

from api.rest.apitestcase import ApiTestCase
from amcat.tools import amcattest, toolkit

class TestSearch(ApiTestCase):
    @amcattest.use_elastic
    def test_dates(self):
        """Test whether date deserialization works, see #66"""
        import datetime
        from amcat.tools import amcates
        for d in ('2001-01-01', '1992-12-31T23:59', '2012-02-29T12:34:56.789', datetime.datetime.now()):
            a = amcattest.create_test_article(date=d)
            amcates.ES().flush()
            res = self.get("/api/v4/search", ids=a.id)
            self.assertEqual(toolkit.readDate(res['results'][0]['date']), toolkit.readDate(str(d)))
