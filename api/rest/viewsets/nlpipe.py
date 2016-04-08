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

"""
API Viewsets for dealing with NLP (pre)processing via nlpipe
"""

from __future__ import unicode_literals, print_function, absolute_import

from collections import namedtuple
import itertools
import json
import tempfile
import logging

from rest_framework.response import Response
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.decorators import api_view
from rest_framework.status import HTTP_200_OK
from rest_framework import serializers
from rest_framework.mixins import ListModelMixin, CreateModelMixin
from rest_framework.viewsets import ModelViewSet, ViewSet, GenericViewSet



from amcat.models import Article, ArticleSet

from api.rest.viewsets.articleset import ArticleSetViewSetMixin
from api.rest.viewsets.project import ProjectViewSetMixin
from api.rest.viewsets.article import ArticleViewSetMixin
from api.rest.mixins import DatatablesMixin

from nlpipe.backend import get_cached_document_ids
from nlpipe.pipeline import get_results
from nlpipe.celery import app

from KafNafParserPy import KafNafParser
from io import BytesIO

def _get_tokens(term_vector, pos_inc=0, offset_inc=0):
    for term, info in term_vector.items():
        for token in info['tokens']:
            token['position'] += pos_inc
            token['start_offset'] += offset_inc
            token['end_offset'] += offset_inc
            token['term'] = term
            yield token

def _tokens_from_vectors(vectors, fields, aid):
    pos_inc, offset_inc = 0, 0
    for field in fields:
        tokens = _get_tokens(vectors[field]['terms'],
                             pos_inc=pos_inc, offset_inc=offset_inc)
        tokens = sorted(tokens, key = lambda t:t['position'])
        pos_inc = (tokens[-1]['position'] + 1) if tokens else 0
        offset_inc = (tokens[-1]['end_offset'] + 1) if tokens else 0
        for token in tokens:
            token['aid'] = aid
            yield token

def _termvector(aid):
    from amcat.tools import amcates
    import collections
    fields = ["headline", "text"]
    res =  amcates.ES().term_vector(aid, fields=fields)
    pos_inc, offset_inc = 0, 0
    for field in fields:
        tokens = _get_tokens(res['term_vectors'][field]['terms'],
                             pos_inc=pos_inc, offset_inc=offset_inc)
        tokens = sorted(tokens, key = lambda t:t['position'])
        pos_inc = (tokens[-1]['position'] + 1) if tokens else 0
        offset_inc = (tokens[-1]['end_offset'] + 1) if tokens else 0
        for token in tokens:
            token['aid'] = aid
            yield token

    
def _termvectors(aids):
    fields = ["headline", "text"]
    for doc in amcates.ES().term_vectors(aids, fields):
        aid = doc['_id']
        yield aid, list(_tokens_from_vectors(doc['term_vectors'], fields, aid))


class NLPipeLemmataSerializer(serializers.Serializer):
    class Meta:
        class list_serializer_class(serializers.ListSerializer):
            def to_representation(self, data):

                if self.context['request'].GET.get('module', 'elastic') == "elastic":
                    self.child._cache = dict(_termvectors(data))
                else:
                    self.child._cache = get_results(data, self.child.module)
                result = serializers.ListSerializer.to_representation(self, data)
                # flatten list of lists
                result = itertools.chain(*result)
                return result

    @property
    def module(self):
        module = self.context['request'].GET.get('module')
        if not module:
            raise ValidationError("Please specify the NLP module to use with a module= GET parameter")
        from nlpipe import tasks
        if not hasattr(tasks, module):
            raise ValidationError("Module {module} not known".format(**locals()))
        
        return getattr(tasks, module)
    
    def to_representation(self, article):
        result = self._cache[str(article)]
        if isinstance(result, list):
            return result
        else:
            return self.from_naf(result.input)
        
    def from_naf(self, naf):
        naf = KafNafParser(BytesIO(naf.encode("utf-8")))

        deps = {dep.get_to(): (dep.get_function(), dep.get_from())
                for dep in naf.get_dependencies()}
        tokendict = {token.get_id(): token for token in naf.get_tokens()}

        for term in naf.get_terms():
            tokens = [tokendict[id] for id in term.get_span().get_span_ids()]
            for token in tokens:
                tid = term.get_id()
                tok = {"aid": article,
                       "token_id": token.get_id(),
                       "offset": token.get_offset(),
                       "sentence": token.get_sent(),
                       "para": token.get_para(),
                       "word": token.get_text(),
                       "term_id": tid,
                       "lemma": term.get_lemma(),
                       "pos": term.get_pos()}
                if tid in deps:
                    rel, parent = deps[tid]
                    tok['parent'] = parent
                    tok['relation'] = rel.split("/")[-1]
                yield tok



class NLPipeLemmataViewSet(ProjectViewSetMixin, ArticleSetViewSetMixin, DatatablesMixin, ModelViewSet):
    model_key = "token"
    model = Article
    queryset = Article.objects.all()
    serializer_class = NLPipeLemmataSerializer

    @property
    def module(self):
        module = self.request.GET.get('module')
        if not module:
            raise ValidationError("Please specify the NLP module to use with a module= GET parameter")
        from nlpipe import tasks
        if not hasattr(tasks, module):
            raise ValidationError("Module {module} not known".format(**locals()))
        
        return getattr(tasks, module)
    
    def filter_queryset(self, queryset):

        logging.info("Getting ids")
        ids = list(self.articleset.get_article_ids_from_elastic())
        logging.info("Got {} ids".format(len(ids)))


        only_cached = self.request.GET.get('only_cached', 'N')
        only_cached = only_cached[0].lower() in ['1', 'y']

        if only_cached:
            logging.info("Filtering ids")
            ids = list(get_cached_document_ids(ids, self.module.doc_type))
            logging.info("{} ids left".format(len(ids)))
            
        
        return ids
              
        queryset = super(NLPipeLemmataViewSet, self).filter_queryset(queryset)
        # only(.) would be better on serializer, but meh
        try:
            queryset = queryset.filter(articlesets_set=self.articleset).only("pk")
        except ArticleSet.DoesNotExist:
            from django.http import Http404
            raise Http404("Articleset does not exist")
        return queryset
    
    def get_renderer_context(self):
        context = super(NLPipeLemmataViewSet, self).get_renderer_context()
        context['fast_csv'] = True 
        return context


ModuleCount = namedtuple("ModuleCount", ["module", "n"])

class PreprocessViewSet(ProjectViewSetMixin, ArticleSetViewSetMixin, DatatablesMixin,
                        ListModelMixin, GenericViewSet):
    model_key = "preproces"
    model = None
    base_name = "preprocess"

    class serializer_class(serializers.Serializer):
        module = serializers.CharField()
        n = serializers.IntegerField()
    
    def filter_queryset(self, queryset):
        return queryset
    
    def get_queryset(self):
        ids = list(self.articleset.get_article_ids_from_elastic())
        result = [ModuleCount("Total #articles", len(ids))]
        from nlpipe.document import count_cached
        for module, n in count_cached(ids):
            result.append(ModuleCount(module, n))

        return result

    def get_filter_fields(self):
        return []
