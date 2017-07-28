from collections import defaultdict
import json
from nose.tools import set_trace
from sqlalchemy.orm.exc import (
    MultipleResultsFound,
    NoResultFound,
)

from model import (
    Place
)

class AuthenticationDocument(object):
    """Parse an Authentication For OPDS document, including the
    Library Simplified-specific extensions, extracting all the information
    that's of interest to the library registry.
    """

    ANONYMOUS_ACCESS_REL = "https://librarysimplified.org/rel/auth/anonymous"

    COVERAGE_EVERYWHERE = "everywhere"
    
    # The list of color schemes supported by SimplyE.
    SIMPLYE_COLOR_SCHEMES = [
        "red", "blue", "gray", "gold", "green", "teal", "purple",
    ]   
    
    PUBLIC_AUDIENCE = 'public'
    AUDIENCES = [PUBLIC_AUDIENCE, 'educational-primary',
                 'educational-secondary', 'research', 'print-disability',
                 'other']
    
    def __init__(self, _db, id, title, type, service_description, color_scheme, 
                 collection_size, public_key, audiences, service_area,
                 focus_area, links, place_class=Place):
        self.id = id
        self.title = title
        self.service_description = service_description
        self.color_scheme = color_scheme
        self.collection_size = collection_size
        self.public_key = public_key
        self.audiences = audiences or [self.PUBLIC_AUDIENCE]
        if service_area:
            self.service_area = self.parse_coverage(
                _db, service_area, place_class=place_class
            )
        else:
            self.service_area = [place_class.everywhere(_db)], [], []
        if focus_area:
            self.focus_area = self.parse_coverage(
                _db, focus_area, place_class=place_class
            )
        else:
            self.focus_area = [place_class.everywhere(_db)], [], []
        self.links = links
        self.website = self.extract_link(
            rel="alternate", require_type="text/html"
        )
        self.registration = self.extract_link(rel="register")
        self.root = self.extract_link(
            rel="start",
            prefer_type="application/atom+xml;profile=opds-catalog"
        )
        logo = self.extract_link(rel="logo")
        self.logo = None
        self.logo_link = None
        if logo:
            data = logo.get('href', '')
            if data and data.startswith('data:'):
                self.logo = data
            else:
                self.logo_link = logo
        self.anonymous_access = False
        if (type == self.ANONYMOUS_ACCESS_REL
            or isinstance(type, list) and self.ANONYMOUS_ACCESS_REL in type):
            self.anonymous_access = True

    def extract_link(self, rel, require_type=None, prefer_type=None):
        return self._extract_link(
            self.links, rel, require_type, prefer_type
        )

    @classmethod
    def parse_coverage(cls, _db, coverage, place_class=Place):
        """Derive Place objects from an Authentication For OPDS coverage
        object (i.e. a value for `service_area` or `focus_area`)

        :param coverage: An Authentication For OPDS coverage object.

        :param place_class: In unit tests, pass in a mock replacement
        for the Place class here.

        :return: A 3-tuple (places, unknown, ambiguous).

        `places` is a list of Place model objects.

        `unknown` is a coverage object representing the subset of
        `coverage` that had no corresponding Place objects.

        `ambiguous` is a coverage object representing
        the subset of `coverage` that had more than one corresponding
        Place object.
        """
        place_objs = []
        unknown = defaultdict(list)
        ambiguous = defaultdict(list)
        if coverage == cls.COVERAGE_EVERYWHERE:
            # This library covers the entire universe! No need to
            # parse anything.
            place_objs.append(place_class.everywhere(_db))
            coverage = dict()
            
        for country, places in coverage.items():
            try:
                country_obj = place_class.lookup_one_by_name(
                    _db, country, place_type=Place.NATION,
                )
                if places == cls.COVERAGE_EVERYWHERE:
                    # This library covers an entire country.
                    place_objs.append(country_obj)
                else:
                    # This library covers a list of places within a
                    # country.
                    if isinstance(places, basestring):
                        # This is invalid -- you're supposed to always
                        # pass in a list -- but we can support it.
                        places = [places]
                    for place in places:
                        try:
                            place_obj = country_obj.lookup_inside(place)
                            if place_obj:
                                # We found it.
                                place_objs.append(place_obj)
                            else:
                                # We couldn't find any place with this name.
                                unknown[country].append(place)
                        except MultipleResultsFound, e:
                            # The place was ambiguously named.
                            ambiguous[country].append(place)
            except MultipleResultsFound, e:
                # A country was ambiguously named -- not very likely.
                ambiguous[country] = places
            except NoResultFound, e:
                # Either this isn't a recognized country
                # or we don't have a geography for it.
                unknown[country] = places

        return place_objs, unknown, ambiguous
    
    @classmethod
    def _extract_link(cls, links, rel, require_type=None, prefer_type=None):
        if not links:
            # There are no links, period.
            return None
        links = links.get(rel)
        if not links:
            # There are no links with this link relation.
            return None
        if not isinstance(links, list):
            # A single link.
            links = [links]
        good_enough = None
        for link in links:
            if not require_type and not prefer_type:
                # Any link with this relation will work. Return the
                # first one we see.
                return link

            # Beyond this point, either require_type or prefer_type is
            # set, so the type of the link becomes relevant.
            type = link.get('type', '')
            
            if type:
                if (require_type and type.startswith(require_type)
                    or prefer_type and type.startswith(prefer_type)):
                    # If we have a require_type, this means we have
                    # met the requirement. If we have a prefer_type,
                    # we will not find a better link than this
                    # one. Return it immediately.
                    return link
            if not require_type and not good_enough:
                # We would prefer a link of a certain type, but if it
                # turns out there is no such link, we will accept the
                # first link of the given type.
                good_enough = link
        return good_enough
            
    @classmethod
    def from_string(cls, _db, s, place_class=Place):
        data = json.loads(s)
        return cls.from_dict(_db, data, place_class)

    @classmethod
    def from_dict(cls, _db, data, place_class=Place):
        return AuthenticationDocument(
            _db,
            id=data.get('id', None),
            title=data.get('title', data.get('name', None)),
            type=data.get('type', []),
            service_description=data.get('service_description', None),
            color_scheme=data.get('color_scheme'),
            collection_size=data.get('collection_size'),
            public_key=data.get('public_key'),
            audiences=data.get('audience'),
            service_area=data.get('service_area'),
            focus_area=data.get('service_area'),
            links=data.get('links', {}),
            place_class=place_class
        )
