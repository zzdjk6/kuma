"""Data aggregators for dashboards"""

from django.conf import settings
from django.db import connections, router
from django.utils.datastructures import SortedDict

import jingo
from tower import ugettext as _, ugettext_lazy as _lazy

from sumo.urlresolvers import reverse
from wiki.models import Document, MEDIUM_SIGNIFICANCE, MAJOR_SIGNIFICANCE


def overview_rows(locale):
    """Return the iterable of dicts needed to draw the Overview table."""
    # The Overview table is a special case: it has only a static number of
    # rows, so it has no expanded, all-rows view, and thus needs no slug, no
    # "max" kwarg on rows(), etc. It doesn't fit the Readout signature, so we
    # don't shoehorn it in.
    total = Document.uncached.exclude(current_revision=None).filter(
                locale=settings.WIKI_DEFAULT_LANGUAGE,
                is_localizable=True).count()

    # How many approved documents are there in German that have parents?
    translated = Document.uncached.filter(locale=locale).exclude(
        current_revision=None).exclude(parent=None).count()

    return [dict(title=_('All Knowledge Base Articles'),
                 numerator=translated, denominator=total,
                 percent=(int(round(translated / float(total) * 100)) if total
                          else 100),
                 description=_('How many of the approved English articles '
                               'which allow translations have an approved '
                               'translation into this language')),
            # TODO: Enable after we integrate WebTrends stats:
#             dict(title='Most Viewed Articles',
#                  numerator=0, denominator=1,
#                  percent=0,
#                  description='These are the top 15-20 most visited English'
#                              ' articles, which in sum account for over 50%'
#                              ' of the total traffic to the English '
#                              'Knowledge Base.')
           ]


class Readout(object):
    """Abstract class representing one table on the Localization Dashboard

    Describing these as atoms gives us the whole-page details views for free.

    """
    #title = _lazy(u'Title of Readout')
    #short_title= = _lazy(u'Short Title of Readout for In-Page Links')
    #slug = 'URL slug for detail page'
    #details_link_text = _lazy(u'All articles from this readout...')
    column4_label = _lazy(u'Status')

    def __init__(self, request, locale=None):
        """Take request so the template can use contextual macros that need it.

        Renders the data for the locale specified by the request, but you can
        override it by passing another in `locale`.

        """
        self.request = request
        self.locale = locale or request.locale

    def rows(self, max=None):
        """Return an iterable of dicts containing the data for the table.

        This default implementation does calls _query_and_params and
        _format_row. You can either implement those or, if you need more
        flexibility, override this.

        Limit to `max` rows.

        """
        cursor = connections[router.db_for_read(Document)].cursor()
        cursor.execute(*self._query_and_params(max))
        return (self._format_row(r) for r in cursor.fetchall())

    def render(self, rows):
        """Return HTML table rows for the given data."""
        return jingo.render_to_string(
            self.request,
            'dashboards/includes/kb_readout.html',
            {'rows': rows, 'column4_label': self.column4_label})

    # To override:

    def _query_and_params(self, max):
        """Return a tuple: (query, params to bind it to)."""
        raise NotImplementedError

    def _format_row(self, row):
        """Turn a DB row tuple into a dict for the template."""
        raise NotImplementedError

    # Convenience methods:

    @staticmethod
    def limit_clause(max):
        """Return a SQL LIMIT clause limiting returned rows to `max`.

        Return '' if max is None.

        """
        return ' LIMIT %i' % max if max else ''


class UntranslatedReadout(Readout):
    title = _lazy(u'Untranslated Articles')
    short_title = _lazy('Untranslated')
    details_link_text = _lazy(u'All untranslated articles...')
    slug = 'untranslated'
    column4_label = _lazy(u'Updated')

    def _query_and_params(self, max):
        # Incidentally, we tried this both as a left join and as a search
        # against an inner query returning translated docs, and the left join
        # yielded a faster-looking plan (on a production corpus).
        return ('SELECT parent.slug, parent.title, '
            'wiki_revision.reviewed '
            'FROM wiki_document parent '
            'INNER JOIN wiki_revision ON '
                'parent.current_revision_id=wiki_revision.id '
            'LEFT OUTER JOIN wiki_document translated ON '
                'parent.id=translated.parent_id AND translated.locale=%s '
            'WHERE '
            'translated.id IS NULL AND parent.is_localizable AND '
            'parent.locale=%s '
            'ORDER BY wiki_revision.reviewed DESC' + self.limit_clause(max),
            [self.locale, settings.WIKI_DEFAULT_LANGUAGE])

    def _format_row(self, (slug, title, reviewed)):
        # Run the data through the model to (potentially) format it and
        # take advantage of SPOTs (like for get_absolute_url()):
        d = Document(slug=slug, title=title,
                     locale=settings.WIKI_DEFAULT_LANGUAGE)
        return (dict(title=d.title,
                     url=d.get_absolute_url(),
                     visits=0, percent=0,
                     updated=reviewed))


class OutOfDateReadout(Readout):
    title = _lazy(u'Out-of-Date Translations')
    short_title = _lazy('Out-of-Date')
    details_link_text = _lazy(u'All out-of-date translations...')
    slug = 'out-of-date'
    column4_label = _lazy(u'Out of date since')

    # To show up in this readout, an article's revision since the last
    # approved translation must have a maximum significance equal to this
    # value:
    _max_significance = MAJOR_SIGNIFICANCE

    def _query_and_params(self, max):
        # At the moment, the "Out of Date Since" column shows the time since
        # the translation was out of date at a MEDIUM level of severity or
        # higher. We could arguably knock this up to MAJOR, but technically it
        # is out of date when the original gets anything more than typo
        # corrections.
        return ('SELECT transdoc.slug, transdoc.title, engrev.reviewed '
            'FROM wiki_document transdoc '
            'INNER JOIN wiki_revision engrev ON engrev.id='
            # The oldest english rev to have an approved level-30 change since
            # the translated doc had an approved rev based on it. NULL if there
            # is none:
                '(SELECT min(id) FROM wiki_revision '
                # Narrow engrev rows to those representing revision of parent
                # doc:
                'WHERE wiki_revision.document_id=transdoc.parent_id '
                # For the purposes of computing the "Out of Date Since" column,
                # the revision that threw the translation out of date had
                # better be more recent than the one the current translation is
                # based on:
                'AND wiki_revision.id>'
                    '(SELECT based_on_id FROM wiki_revision basedonrev '
                    'WHERE basedonrev.id=transdoc.current_revision_id) '
                'AND wiki_revision.significance>=%s '
                'AND %s='
                # Completely filter out outer selections where 30 is not the
                # max signif of english revisions since trans was last
                # approved. Other maxes will be shown by other readouts.
                # Optimize: try "30 IN" if MySQL's statistics gatherer is
                # stupid/nonexistent; the inner query should be able to bail
                # out early. [Ed: No effect on EXPLAIN on admittedly light test
                # corpus.]
                  '(SELECT MAX(engsince.significance) '
                  'FROM wiki_revision engsince '
                  'WHERE engsince.document_id=transdoc.parent_id '
                  # Assumes that any approved revision became the current
                  # revision at some point: we don't let the user go back and
                  # approve revisions older than the latest approved one.
                  'AND engsince.is_approved '
                  'AND engsince.id>'
                  # The English revision the current translation's based on:
                    '(SELECT based_on_id FROM wiki_revision basedonrev '
                    'WHERE basedonrev.id=transdoc.current_revision_id) '
                  ')'
                ') '
            'WHERE transdoc.locale=%s '
            'ORDER BY engrev.reviewed DESC' + self.limit_clause(max),
            [MEDIUM_SIGNIFICANCE, self._max_significance, self.locale])

    def _format_row(self, (slug, title, reviewed)):
        return (dict(title=title,
                     url=reverse('wiki.edit_document', args=[slug]),
                     visits=0, percent=0,
                     updated=reviewed))


class NeedingUpdatesReadout(OutOfDateReadout):
    title = _lazy(u'Translations Needing Updates')
    short_title = _lazy('Needing Updates')
    details_link_text = _lazy(u'All translations needing updates...')
    slug = 'needing-updates'

    _max_significance = MEDIUM_SIGNIFICANCE


class UnreviewedReadout(Readout):
    title = _lazy(u'Unreviewed Changes')
    # ^ Not just changes to translations but also unreviewed chanages to docs
    # in this locale that are not translations

    short_title = _lazy('Unreviewed')
    details_link_text = _lazy(u'All articles requiring review...')
    slug = 'unreviewed'
    column4_label = _lazy(u'Changed')

    def _query_and_params(self, max):
        return ('SELECT wiki_document.slug, wiki_document.title, '
            'MAX(wiki_revision.created) maxcreated, '
            'GROUP_CONCAT(DISTINCT auth_user.username '
                         "ORDER BY wiki_revision.id SEPARATOR ', ') "
            'FROM wiki_document '
            'INNER JOIN wiki_revision ON '
                        'wiki_document.id=wiki_revision.document_id '
            'INNER JOIN auth_user ON wiki_revision.creator_id=auth_user.id '
            'WHERE wiki_revision.reviewed IS NULL '
            'AND (wiki_document.current_revision_id IS NULL OR '
                 'wiki_revision.id>wiki_document.current_revision_id) '
            'AND wiki_document.locale=%s '
            'GROUP BY wiki_document.id '
            'ORDER BY maxcreated DESC' + self.limit_clause(max),
            [self.locale])

    def _format_row(self, (slug, title, changed, users)):
        return (dict(title=title,
                     url=reverse('wiki.document_revisions', args=[slug]),
                     visits=0, percent=0,
                     updated=changed,
                     users=users))


# L10n Dashboard tables that have their own whole-page views:
L10N_READOUTS = SortedDict((t.slug, t) for t in
    [UntranslatedReadout, OutOfDateReadout, NeedingUpdatesReadout,
    UnreviewedReadout])

# Contributors ones:
CONTRIBUTOR_READOUTS = dict((t.slug, t) for t in [UnreviewedReadout])