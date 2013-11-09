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

from amcat.models import Article, ArticleSet, Medium
from api.rest.resources.amcatresource import AmCATResource
from api.rest.resources.articleset import ArticleSetViewSet

from api.rest.serializer import AmCATModelSerializer
from api.rest.filters import AmCATFilterSet, InFilter
from rest_framework.viewsets import ModelViewSet
from api.rest.resources.amcatresource import DatatablesMixin

from api.rest.viewsets import (ProjectViewSetMixin, ROLE_PROJECT_READER,
                               CannotEditLinkedResource, NotFoundInProject)

from rest_framework import serializers
from django_filters import filters, filterset
import logging
log = logging.getLogger(__name__)

class ArticleMetaFilter(AmCATFilterSet):
    date_from = filters.DateFilter(name='date', lookup_type='gte')
    date_to = filters.DateFilter(name='date', lookup_type='lt')
    articleset = InFilter(name='articlesets_set', queryset=ArticleSet.objects.all())
    
    class Meta:
        model = Article
        order_by=True
        
class ArticleMetaSerializer(AmCATModelSerializer):
    class Meta:
        model = Article
        fields = ("id", "date", "project", "medium", "headline",
                    "section", "pagenr", "author", "length")

class ArticleMetaResource(AmCATResource):
    model = Article
    serializer_class = ArticleMetaSerializer
    filter_class = ArticleMetaFilter
    
    @classmethod
    def get_model_name(cls):
        return "ArticleMeta".lower()

class ArticleSerializer(AmCATModelSerializer):

    def restore_fields(self, data, files):
        # convert media from name to id, if needed
        try:
            int(data['medium'])
        except ValueError:
            # medium was name instead of int
            if not hasattr(self, 'media'):
                self.media = {}
                m = data['medium']
                if m not in self.media:
                    self.media[m] = Medium.get_or_create(m).id
                data['medium'] = self.media[m]

        return super(ArticleSerializer, self).restore_fields(data, files)
                
    def save(self, **kwargs):
        articles = self.object if isinstance(self.object, list) else [self.object]
        Article.create_articles(articles, self.context['view'].articleset)
        return self.object
    class Meta:
        model = Article
        
class ArticleViewSet(ProjectViewSetMixin, DatatablesMixin, ModelViewSet):
    model = Article
    url = ArticleSetViewSet.url + '/(?P<articleset>[0-9]+)/articles'
    permission_map = {'GET' : ROLE_PROJECT_READER}
    serializer_class = ArticleSerializer
    
    def check_permissions(self, request):
        # make sure that the requested set is available in the projec, raise 404 otherwiset
        # sets linked_set to indicate whether the current set is owned by the project
        if self.articleset.project == self.project:
            pass
        elif self.project.articlesets.filter(pk=self.articleset.id).exists():
            if request.method == 'POST':
                raise CannotEditLinkedResource()
        else:
            raise NotFoundInProject()
        return super(ArticleViewSet, self).check_permissions(request)
    
    
    @property
    def articleset(self):
        if not hasattr(self, '_articleset'):
            articleset_id = int(self.kwargs['articleset'])
            self._articleset = ArticleSet.objects.get(pk=articleset_id)
        return self._articleset

    def filter_queryset(self, queryset):
        queryset = super(ArticleViewSet, self).filter_queryset(queryset)
        return queryset.filter(articlesets_set=self.articleset)

            
###########################################################################
#                          U N I T   T E S T S                            #
###########################################################################

from api.rest.apitestcase import ApiTestCase
from amcat.tools import amcattest
from rest_framework.authentication import SessionAuthentication, BasicAuthentication

class TestArticle(ApiTestCase):
    authentication_classes = (SessionAuthentication, BasicAuthentication)

    
    @amcattest.use_elastic
    def test_create(self):
        s = amcattest.create_test_set()
                            
        # is the set empty? (aka can we get the results)
        url = '/api/v4/projects/{s.project.id}/sets/{s.id}/articles/'.format(**locals())
        url = ArticleViewSet.get_url(project=s.project.id, articleset=s.id)
        result = self.get(url)
        self.assertEqual(result['results'], [])

        body = {'text' : 'bla', 'headline' : 'headline', 'date' : '2013-01-01T00:00:00', 'medium' : 'test_medium'}
        
        result = self.post(url, body, as_user=s.project.owner)
        self.assertEqual(result['headline'], body['headline'])
        
        result = self.get(url)
        self.assertEqual(len(result['results']), 1)
        a = result['results'][0]
        self.assertEqual(a['headline'], body['headline'])
        self.assertEqual(a['project'], s.project_id)
        self.assertEqual(a['length'], 2)

        # Is the result added to the elastic index as well?
        from amcat.tools import amcates
        amcates.ES().flush()
        r = list(amcates.ES().query(filters=dict(sets=s.id), fields=["text", "headline", 'medium']))
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0].medium, "test_medium")
        self.assertEqual(r[0].headline, "headline") 
        
    def test_permissions(self):
        from amcat.models import Role, ProjectRole
        metareader = Role.objects.get(label='metareader', projectlevel=True)
        reader = Role.objects.get(label='reader', projectlevel=True)
        
        p1 = amcattest.create_test_project(guest_role=None)
        p2 = amcattest.create_test_project(guest_role=metareader)
        
        s1 = amcattest.create_test_set(project=p1)        
        s2 = amcattest.create_test_set(project=p2)

        p1.articlesets.add(s2)
        #alias
        url, set_url = ArticleViewSet.get_url, ArticleSetViewSet.get_url

        body = {'text' : 'bla', 'headline' : 'headline', 'date' : '2013-01-01T00:00:00', 'medium' : 'test_medium'}
        # anonymous user shoud be able to read p2's articlesets but not articles (requires READER), and nothing on p1
                                                  
        self.get(url(project=p1.id, articleset=s1.id), check_status=401)
        self.get(url(project=p2.id, articleset=s2.id), check_status=401)

        self.get(set_url(project=p1.id), check_status=401)
        self.get(set_url(project=p2.id), check_status=200)

        # it is illegal to view an articleset through a project it is not a member of
        self.get(url(project=p2.id, articleset=s1.id), check_status=404)
        
        u = p1.owner
        ProjectRole.objects.create(project=p2, user=u, role=reader)

        # User u shoud be able to view all views
        self.get(url(project=p1.id, articleset=s1.id), as_user=u, check_status=200)
        self.get(url(project=p1.id, articleset=s2.id), as_user=u, check_status=200)
        self.get(url(project=p2.id, articleset=s2.id), as_user=u, check_status=200)
        # Except this one, of course, because it doesn't exist
        self.get(url(project=p2.id, articleset=s1.id), as_user=u, check_status=404)

        self.get(set_url(project=p1.id), as_user=u, check_status=200)
        self.get(set_url(project=p2.id), as_user=u, check_status=200)

        # User u should be able to add articles to set 1 via project 1, but not p2/s2
        self.post(url(project=p1.id, articleset=s1.id), body, as_user=u, check_status=201)
        self.post(url(project=p2.id, articleset=s2.id), body, as_user=u, check_status=403)
        
        # Neither u (p1.owner) nor p2.owner should be able to modify set 2 via project 1
        self.post(url(project=p1.id, articleset=s2.id), body, as_user=u, check_status=403)
        self.post(url(project=p1.id, articleset=s2.id), body, as_user=p2.owner, check_status=403)
