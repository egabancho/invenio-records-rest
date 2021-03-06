# -*- coding: utf-8 -*-
#
# This file is part of Invenio.
# Copyright (C) 2015, 2016 CERN.
#
# Invenio is free software; you can redistribute it
# and/or modify it under the terms of the GNU General Public License as
# published by the Free Software Foundation; either version 2 of the
# License, or (at your option) any later version.
#
# Invenio is distributed in the hope that it will be
# useful, but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Invenio; if not, write to the
# Free Software Foundation, Inc., 59 Temple Place, Suite 330, Boston,
# MA 02111-1307, USA.
#
# In applying this license, CERN does not
# waive the privileges and immunities granted to it by virtue of its status
# as an Intergovernmental Organization or submit itself to any jurisdiction.

"""REST API resources."""

from __future__ import absolute_import, print_function

import uuid
from functools import partial, wraps

from flask import Blueprint, abort, current_app, jsonify, make_response, \
    request, url_for
from invenio_db import db
from invenio_pidstore import current_pidstore
from invenio_pidstore.errors import PIDDeletedError, PIDDoesNotExistError, \
    PIDMissingObjectError, PIDRedirectedError, PIDUnregistered
from invenio_pidstore.models import PersistentIdentifier
from invenio_pidstore.resolver import Resolver
from invenio_search import Query, current_search_client
from invenio_records.api import Record
from invenio_rest import ContentNegotiatedMethodView
from invenio_rest.decorators import require_content_types
from jsonpatch import JsonPatchException, JsonPointerException
from sqlalchemy.exc import SQLAlchemyError
from werkzeug.local import LocalProxy
from werkzeug.routing import BuildError
from werkzeug.utils import import_string

current_records_rest = LocalProxy(
    lambda: current_app.extensions['invenio-records-rest'])


def create_blueprint(endpoints):
    """Create Invenio-Records-REST blueprint."""
    blueprint = Blueprint(
        'invenio_records_rest',
        __name__,
        url_prefix='',
    )

    for endpoint, options in (endpoints or {}).items():
        for rule in create_url_rules(endpoint, **options):
            blueprint.add_url_rule(**rule)

    return blueprint


def create_url_rules(endpoint, list_route=None, item_route=None,
                     pid_type=None, pid_minter=None, pid_fetcher=None,
                     read_permission_factory_imp=None,
                     create_permission_factory_imp=None,
                     update_permission_factory_imp=None,
                     delete_permission_factory_imp=None,
                     record_serializers=None, search_serializers=None,
                     search_index=None, search_type=None):
    """Create Werkzeug URL rules.

    :param endpoint: Name of endpoint.
    :param list_route: record listing URL route . Required.
    :param item_route: record URL route (must include ``<pid_value>`` pattern).
        Required.
    :param pid_type: Persistent identifier type for endpoint. Required.
    :param template: Template to render. Defaults to
        ``invenio_records_ui/detail.html``.
    :param read_permission_factory: Import path to factory that creates a read
        permission object for a given record.
    :param create_permission_factory: Import path to factory that creates a
        create permission object for a given record.
    :param update_permission_factory: Import path to factory that creates a
        update permission object for a given record.
    :param delete_permission_factory: Import path to factory that creates a
        delete permission object for a given record.
    :param search_index: Name of the search index used when searching records.
    :param search_type: Name of the search type used when searching records.
    :param record_serializers: serializers used for records.
    :param search_serializers: serializers used for search results.

    :returns: a list of dictionaries with can each be passed as keywords
        arguments to ``Blueprint.add_url_rule``.
    """
    assert list_route
    assert item_route
    assert pid_type
    assert search_serializers
    assert record_serializers
    assert search_index

    read_permission_factory = import_string(read_permission_factory_imp) \
        if read_permission_factory_imp else None
    create_permission_factory = import_string(create_permission_factory_imp) \
        if create_permission_factory_imp else None
    update_permission_factory = import_string(update_permission_factory_imp) \
        if update_permission_factory_imp else None
    delete_permission_factory = import_string(delete_permission_factory_imp) \
        if delete_permission_factory_imp else None

    # import the serializers
    record_serializers = {mime: import_string(func) for mime, func in
                          record_serializers.items()}
    search_serializers = {mime: import_string(func) for mime, func in
                          search_serializers.items()}

    resolver = Resolver(pid_type=pid_type, object_type='rec',
                        getter=partial(Record.get_record, with_deleted=True))

    list_view = RecordsListResource.as_view(
        RecordsListResource.view_name.format(endpoint),
        resolver=resolver,
        minter_name=pid_minter,
        pid_type=pid_type,
        pid_fetcher=pid_fetcher,
        read_permission_factory=read_permission_factory,
        create_permission_factory=create_permission_factory,
        record_serializers=record_serializers,
        search_serializers=search_serializers,
        search_index=search_index,
        search_type=search_type)
    item_view = RecordResource.as_view(
        RecordResource.view_name.format(endpoint),
        resolver=resolver,
        read_permission_factory=read_permission_factory,
        update_permission_factory=update_permission_factory,
        delete_permission_factory=delete_permission_factory,
        serializers=record_serializers)

    return [
        dict(rule=list_route, view_func=list_view),
        dict(rule=item_route, view_func=item_view),
    ]


def pass_record(f):
    """Decorator to retrieve persistent identifier and record."""
    @wraps(f)
    def inner(self, pid_value, *args, **kwargs):
        try:
            pid, record = self.resolver.resolve(pid_value)
        except (PIDDoesNotExistError, PIDUnregistered):
            abort(404)
        except PIDDeletedError:
            abort(410)
        except PIDMissingObjectError as e:
            current_app.logger.exception(
                'No object assigned to {0}.'.format(e.pid),
                extra={'pid': e.pid})
            abort(500)
        except PIDRedirectedError as e:
            try:
                location = url_for(
                    'invenio_records_rest.{0}_item'.format(
                        e.destination_pid.pid_type),
                    pid_value=e.destination_pid.pid_value)
                data = dict(
                    status=301,
                    message='Moved Permanently',
                    location=location,
                )
                response = make_response(jsonify(data), data['status'])
                response.headers['Location'] = location
                return response
            except BuildError:
                current_app.logger.exception(
                    'Invalid redirect - pid_type "{0}" '
                    'endpoint missing.'.format(
                        e.destination_pid.pid_type),
                    extra={
                        'pid': e.pid,
                        'destination_pid': e.destination_pid,
                    })
                abort(500)

        return f(self, pid=pid, record=record, *args, **kwargs)
    return inner


def verify_record_permission(permission_factory, record):
    """Check that the current user has the required permissions on record.

    :param permission_factory: permission factory used to check permissions.
    :param record: record whose access is limited.
    """
    # Note, cannot be done in one line due overloading of boolean
    # operations permission object.
    if not permission_factory(record).can():
        from flask_login import current_user
        if not current_user.is_authenticated:
            abort(401)
        abort(403)


def need_record_permission(factory_name):
    """Decorator checking that the user has the required permissions on record.

    :param factory_name: name of the factory to retrieve.
    """
    def need_record_permission_builder(f):
        @wraps(f)
        def need_record_permission_decorator(self, record, *args, **kwargs):
            permission_factory = (
                getattr(self, factory_name) or
                getattr(current_records_rest, factory_name)
            )
            if permission_factory:
                verify_record_permission(permission_factory, record)
            return f(self, record=record, *args, **kwargs)
        return need_record_permission_decorator
    return need_record_permission_builder


class RecordsListResource(ContentNegotiatedMethodView):
    """Resource for records listing."""

    view_name = '{0}_list'

    def __init__(self, resolver=None, minter_name=None, pid_type=None,
                 pid_fetcher=None, read_permission_factory=None,
                 create_permission_factory=None,
                 search_index=None, search_type=None,
                 record_serializers=None,
                 search_serializers=None, **kwargs):
        """Constructor."""
        super(RecordsListResource, self).__init__(
            method_serializers={
                'GET': search_serializers,
                'POST': record_serializers,
            },
            **kwargs)
        self.resolver = resolver
        self.pid_type = pid_type
        self.minter = current_pidstore.minters[minter_name]
        self.pid_fetcher = current_pidstore.fetchers[pid_fetcher]
        self.read_permission_factory = read_permission_factory
        self.create_permission_factory = create_permission_factory
        self.search_index = search_index
        self.search_type = search_type

    def get(self, **kwargs):
        """Search records.

        :returns: the search result containing hits and aggregations as
        returned by invenio-search.
        """
        page = request.values.get('page', 1, type=int)
        size = request.values.get('size', 10, type=int)
        sort = request.values.get('sort', '', type=str)
        query = Query(request.values.get('q', ''))[(page-1)*size:page*size]

        for sort_key in sort.split(','):
            if sort_key:
                query = query.sort(sort_key)

        response = current_search_client.search(
            index=self.search_index,
            doc_type=self.search_type,
            body=query.body,
            version=True,
        )
        links = {}
        if page > 1:
            links['prev'] = url_for(
                'invenio_records_rest.{0}_list'.format(self.pid_type),
                page=page - 1,
                size=size,
                sort=sort,
                q=request.values.get('q', ''),
                _external=True,
            )
        if size * page < int(response['hits']['total']):
            links['next'] = url_for(
                'invenio_records_rest.{0}_list'.format(self.pid_type),
                page=page + 1,
                size=size,
                sort=sort,
                q=request.values.get('q', ''),
                _external=True,
            )

        return self.make_response(
            pid_fetcher=self.pid_fetcher,
            search_result=response,
            links=links,
        )

    def post(self, **kwargs):
        """Create a record.

        :returns: The created record.
        """
        if request.content_type != 'application/json':
            abort(415)

        # TODO: accept non json content (MARC21...)
        data = request.get_json()
        if data is None:
            return abort(400)

        try:
            # Create uuid for record
            record_uuid = uuid.uuid4()
            # Create persistent identifier
            pid = self.minter(record_uuid, data=data)
            # Create record
            record = Record.create(data, id_=record_uuid)

            # Check permissions
            permission_factory = self.create_permission_factory or \
                current_records_rest.create_permission_factory
            if permission_factory:
                verify_record_permission(permission_factory, record)

            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            current_app.logger.exception('Failed to create record.')
            abort(500)
        return self.make_response(pid, record, 201)


class RecordResource(ContentNegotiatedMethodView):
    """Resource for record items."""

    view_name = '{0}_item'

    def __init__(self, resolver=None, read_permission_factory=None,
                 update_permission_factory=None,
                 delete_permission_factory=None, **kwargs):
        """Constructor.

        :param resolver: Persistent identifier resolver instance.
        """
        super(RecordResource, self).__init__(method_serializers={
            'DELETE': {
                '*/*': lambda *args: make_response(*args),
            },
        }, **kwargs)
        self.resolver = resolver
        self.read_permission_factory = read_permission_factory
        self.update_permission_factory = update_permission_factory
        self.delete_permission_factory = delete_permission_factory

    @pass_record
    @need_record_permission('delete_permission_factory')
    def delete(self, pid, record, **kwargs):
        """Delete a record.

        :param pid: Persistent identifier for record.
        :param record: Record object.
        """
        self.check_etag(str(record.model.version_id))

        try:
            record.delete()
            # mark all PIDs as DELETED
            all_pids = PersistentIdentifier.query.filter(
                PersistentIdentifier.object_type == pid.object_type,
                PersistentIdentifier.object_uuid == pid.object_uuid,
            ).all()
            for rec_pid in all_pids:
                if not rec_pid.is_deleted():
                    rec_pid.delete()
            db.session.commit()
        except Exception:
            db.session.rollback()
            current_app.logger.exception('Failed to delete record.')
            abort(500)
        return '', 204

    @pass_record
    @need_record_permission('read_permission_factory')
    def get(self, pid, record, **kwargs):
        """Get a record.

        :param pid: Persistent identifier for record.
        :param record: Record object.
        :returns: The requested record.
        """
        self.check_etag(str(record.revision_id))
        return pid, record

    @require_content_types('application/json-patch+json')
    @pass_record
    @need_record_permission('update_permission_factory')
    def patch(self, pid, record, **kwargs):
        """Modify a record.

        The data should be a JSON-patch, which will be applied to the record.

        :param pid: Persistent identifier for record.
        :param record: Record object.
        :returns: The modified record.
        """
        # TODO: accept 'application/json' mediatype and use the object
        # to replace the specified attributes
        data = request.get_json(force=True)
        if data is None:
            abort(400)

        self.check_etag(str(record.revision_id))
        try:
            record = record.patch(data)
        except (JsonPatchException, JsonPointerException):
            abort(400)

        record.commit()
        db.session.commit()
        return pid, record

    @require_content_types('application/json')
    @pass_record
    @need_record_permission('update_permission_factory')
    def put(self, pid, record, **kwargs):
        """Replace a record.

        The body should be a JSON object, which will fully replace the current
        record metadata.

        :param pid: Persistent identifier for record.
        :param record: Record object.
        :returns: The modified record.
        """
        # TODO: accept non json content (MARC21...)
        data = request.get_json()
        if data is None:
            abort(400)
        self.check_etag(str(record.revision_id))
        record.clear()
        record.update(data)
        record.commit()
        db.session.commit()
        return self.make_response(pid, record)
