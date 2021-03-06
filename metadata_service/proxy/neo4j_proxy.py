import logging
import textwrap
import time
from random import randint
from typing import (Any, Dict, List, Optional, Tuple, Union,  # noqa: F401
                    no_type_check)

from amundsen_common.models.popular_table import PopularTable
from amundsen_common.models.table import (Application, Column, Reader, Source,
                                          Statistics, Table, User,
                                          Watermark, ProgrammaticDescription)
from amundsen_common.models.table import Tag
from amundsen_common.models.user import User as UserEntity
from beaker.cache import CacheManager
from beaker.util import parse_cache_config_options
from neo4j.v1 import BoltStatementResult, Driver, GraphDatabase  # noqa: F401

from metadata_service.entity.dashboard_detail import DashboardDetail as DashboardDetailEntity
from metadata_service.entity.description import Description
from metadata_service.entity.resource_type import ResourceType
from metadata_service.entity.tag_detail import TagDetail
from metadata_service.exception import NotFoundException
from metadata_service.proxy.base_proxy import BaseProxy
from metadata_service.proxy.statsd_utilities import timer_with_counter
from metadata_service.util import UserResourceRel

_CACHE = CacheManager(**parse_cache_config_options({'cache.type': 'memory'}))

# Expire cache every 11 hours + jitter
_GET_POPULAR_TABLE_CACHE_EXPIRY_SEC = 11 * 60 * 60 + randint(0, 3600)

LOGGER = logging.getLogger(__name__)


class Neo4jProxy(BaseProxy):
    """
    A proxy to Neo4j (Gateway to Neo4j)
    """

    def __init__(self, *,
                 host: str,
                 port: int,
                 user: str = 'neo4j',
                 password: str = '',
                 num_conns: int = 50,
                 max_connection_lifetime_sec: int = 100) -> None:
        """
        There's currently no request timeout from client side where server
        side can be enforced via "dbms.transaction.timeout"
        By default, it will set max number of connections to 50 and connection time out to 10 seconds.
        :param endpoint: neo4j endpoint
        :param num_conns: number of connections
        :param max_connection_lifetime_sec: max life time the connection can have when it comes to reuse. In other
        words, connection life time longer than this value won't be reused and closed on garbage collection. This
        value needs to be smaller than surrounding network environment's timeout.
        """
        endpoint = f'{host}:{port}'
        self._driver = GraphDatabase.driver(endpoint, max_connection_pool_size=num_conns,
                                            connection_timeout=10,
                                            max_connection_lifetime=max_connection_lifetime_sec,
                                            auth=(user, password))  # type: Driver

    @timer_with_counter
    def get_table(self, *, table_uri: str) -> Table:
        """
        :param table_uri: Table URI
        :return:  A Table object
        """

        cols, last_neo4j_record = self._exec_col_query(table_uri)

        readers = self._exec_usage_query(table_uri)

        wmk_results, table_writer, timestamp_value, owners, tags, source, badges, prog_descs = \
            self._exec_table_query(table_uri)

        table = Table(database=last_neo4j_record['db']['name'],
                      cluster=last_neo4j_record['clstr']['name'],
                      schema=last_neo4j_record['schema']['name'],
                      name=last_neo4j_record['tbl']['name'],
                      tags=tags,
                      badges=badges,
                      description=self._safe_get(last_neo4j_record, 'tbl_dscrpt', 'description'),
                      columns=cols,
                      owners=owners,
                      table_readers=readers,
                      watermarks=wmk_results,
                      table_writer=table_writer,
                      last_updated_timestamp=timestamp_value,
                      source=source,
                      is_view=self._safe_get(last_neo4j_record, 'tbl', 'is_view'),
                      programmatic_descriptions=prog_descs
                      )

        return table

    @timer_with_counter
    def _exec_col_query(self, table_uri: str) -> Tuple:
        # Return Value: (Columns, Last Processed Record)

        column_level_query = textwrap.dedent("""
        MATCH (db:Database)-[:CLUSTER]->(clstr:Cluster)-[:SCHEMA]->(schema:Schema)
        -[:TABLE]->(tbl:Table {key: $tbl_key})-[:COLUMN]->(col:Column)
        OPTIONAL MATCH (tbl)-[:DESCRIPTION]->(tbl_dscrpt:Description)
        OPTIONAL MATCH (col:Column)-[:DESCRIPTION]->(col_dscrpt:Description)
        OPTIONAL MATCH (col:Column)-[:STAT]->(stat:Stat)
        RETURN db, clstr, schema, tbl, tbl_dscrpt, col, col_dscrpt, collect(distinct stat) as col_stats
        ORDER BY col.sort_order;""")

        tbl_col_neo4j_records = self._execute_cypher_query(
            statement=column_level_query, param_dict={'tbl_key': table_uri})
        cols = []
        last_neo4j_record = None
        for tbl_col_neo4j_record in tbl_col_neo4j_records:
            # Getting last record from this for loop as Neo4j's result's random access is O(n) operation.
            col_stats = []
            for stat in tbl_col_neo4j_record['col_stats']:
                col_stat = Statistics(
                    stat_type=stat['stat_name'],
                    stat_val=stat['stat_val'],
                    start_epoch=int(float(stat['start_epoch'])),
                    end_epoch=int(float(stat['end_epoch']))
                )
                col_stats.append(col_stat)

            last_neo4j_record = tbl_col_neo4j_record
            col = Column(name=tbl_col_neo4j_record['col']['name'],
                         description=self._safe_get(tbl_col_neo4j_record, 'col_dscrpt', 'description'),
                         col_type=tbl_col_neo4j_record['col']['type'],
                         sort_order=int(tbl_col_neo4j_record['col']['sort_order']),
                         stats=col_stats)

            cols.append(col)

        if not cols:
            raise NotFoundException('Table URI( {table_uri} ) does not exist'.format(table_uri=table_uri))

        return sorted(cols, key=lambda item: item.sort_order), last_neo4j_record

    @timer_with_counter
    def _exec_usage_query(self, table_uri: str) -> List[Reader]:
        # Return Value: List[Reader]

        usage_query = textwrap.dedent("""\
        MATCH (user:User)-[read:READ]->(table:Table {key: $tbl_key})
        RETURN user.email as email, read.read_count as read_count, table.name as table_name
        ORDER BY read.read_count DESC LIMIT 5;
        """)

        usage_neo4j_records = self._execute_cypher_query(statement=usage_query,
                                                         param_dict={'tbl_key': table_uri})
        readers = []  # type: List[Reader]
        for usage_neo4j_record in usage_neo4j_records:
            reader = Reader(user=User(email=usage_neo4j_record['email']),
                            read_count=usage_neo4j_record['read_count'])
            readers.append(reader)

        return readers

    @timer_with_counter
    def _exec_table_query(self, table_uri: str) -> Tuple:
        """
        Queries one Cypher record with watermark list, Application,
        ,timestamp, owner records and tag records.
        """

        # Return Value: (Watermark Results, Table Writer, Last Updated Timestamp, owner records, tag records)

        table_level_query = textwrap.dedent("""\
        MATCH (tbl:Table {key: $tbl_key})
        OPTIONAL MATCH (wmk:Watermark)-[:BELONG_TO_TABLE]->(tbl)
        OPTIONAL MATCH (application:Application)-[:GENERATES]->(tbl)
        OPTIONAL MATCH (tbl)-[:LAST_UPDATED_AT]->(t:Timestamp)
        OPTIONAL MATCH (owner:User)<-[:OWNER]-(tbl)
        OPTIONAL MATCH (tbl)-[:TAGGED_BY]->(tag:Tag{tag_type: $tag_normal_type})
        OPTIONAL MATCH (tbl)-[:TAGGED_BY]->(badge:Tag{tag_type: $tag_badge_type})
        OPTIONAL MATCH (tbl)-[:SOURCE]->(src:Source)
        OPTIONAL MATCH (tbl)-[:DESCRIPTION]->(prog_descriptions:Programmatic_Description)
        RETURN collect(distinct wmk) as wmk_records,
        application,
        t.last_updated_timestamp as last_updated_timestamp,
        collect(distinct owner) as owner_records,
        collect(distinct tag) as tag_records,
        collect(distinct badge) as badge_records,
        src,
        collect(distinct prog_descriptions) as prog_descriptions
        """)

        table_records = self._execute_cypher_query(statement=table_level_query,
                                                   param_dict={'tbl_key': table_uri,
                                                               'tag_normal_type': 'default',
                                                               'tag_badge_type': 'badge'})

        table_records = table_records.single()

        wmk_results = []
        table_writer = None

        wmk_records = table_records['wmk_records']

        for record in wmk_records:
            if record['key'] is not None:
                watermark_type = record['key'].split('/')[-2]
                wmk_result = Watermark(watermark_type=watermark_type,
                                       partition_key=record['partition_key'],
                                       partition_value=record['partition_value'],
                                       create_time=record['create_time'])
                wmk_results.append(wmk_result)

        tags = []
        if table_records.get('tag_records'):
            tag_records = table_records['tag_records']
            for record in tag_records:
                tag_result = Tag(tag_name=record['key'],
                                 tag_type=record['tag_type'])
                tags.append(tag_result)

        badges = []
        if table_records.get('badge_records'):
            badge_records = table_records['badge_records']
            for record in badge_records:
                badge_result = Tag(tag_name=record['key'],
                                   tag_type=record['tag_type'])
                badges.append(badge_result)

        application_record = table_records['application']
        if application_record is not None:
            table_writer = Application(
                application_url=application_record['application_url'],
                description=application_record['description'],
                name=application_record['name'],
                id=application_record.get('id', '')
            )

        timestamp_value = table_records['last_updated_timestamp']

        owner_record = []

        for owner in table_records.get('owner_records', []):
            owner_record.append(User(email=owner['email']))

        src = None

        if table_records['src']:
            src = Source(source_type=table_records['src']['source_type'],
                         source=table_records['src']['source'])

        prog_descriptions = self._extract_programmatic_descriptions_from_query(
            table_records.get('prog_descriptions', [])
        )

        return wmk_results, table_writer, timestamp_value, owner_record, tags, src, badges, prog_descriptions

    def _extract_programmatic_descriptions_from_query(self, raw_prog_descriptions: dict) -> list:
        prog_descriptions = []
        for prog_description in raw_prog_descriptions:
            source = prog_description['description_source']
            if source is None:
                LOGGER.error("A programmatic description with no source was found... skipping.")
            else:
                prog_descriptions.append(ProgrammaticDescription(source=source, text=prog_description['description']))
        prog_descriptions.sort(key=lambda x: x.source)
        return prog_descriptions

    @no_type_check
    def _safe_get(self, dct, *keys):
        """
        Helper method for getting value from nested dict. This also works either key does not exist or value is None.
        :param dct:
        :param keys:
        :return:
        """
        for key in keys:
            dct = dct.get(key)
            if dct is None:
                return None
        return dct

    @timer_with_counter
    def _execute_cypher_query(self, *,
                              statement: str,
                              param_dict: Dict[str, Any]) -> BoltStatementResult:
        if LOGGER.isEnabledFor(logging.DEBUG):
            LOGGER.debug('Executing Cypher query: {statement} with params {params}: '.format(statement=statement,
                                                                                             params=param_dict))
        start = time.time()
        try:
            with self._driver.session() as session:
                return session.run(statement, **param_dict)

        finally:
            # TODO: Add support on statsd
            if LOGGER.isEnabledFor(logging.DEBUG):
                LOGGER.debug('Cypher query execution elapsed for {} seconds'.format(time.time() - start))

    @timer_with_counter
    def _get_resource_description(self, *,
                                  resource_type: ResourceType,
                                  uri: str) -> Description:
        """
        Get the resource description based on the uri. Any exception will propagate back to api server.

        :param resource_type:
        :param id:
        :return:
        """

        description_query = textwrap.dedent("""
        MATCH (n:{node_label} {{key: $key}})-[:DESCRIPTION]->(d:Description)
        RETURN d.description AS description;
        """.format(node_label=resource_type.name))

        result = self._execute_cypher_query(statement=description_query,
                                            param_dict={'key': uri})

        result = result.single()
        return Description(description=result['description'] if result else None)

    @timer_with_counter
    def get_table_description(self, *,
                              table_uri: str) -> Union[str, None]:
        """
        Get the table description based on table uri. Any exception will propagate back to api server.

        :param table_uri:
        :return:
        """

        return self._get_resource_description(resource_type=ResourceType.Table, uri=table_uri).description

    @timer_with_counter
    def _put_resource_description(self, *,
                                  resource_type: ResourceType,
                                  uri: str,
                                  description: str) -> None:
        """
        Update table description with one from user
        :param table_uri: Table uri (key in Neo4j)
        :param description: new value for table description
        """
        # start neo4j transaction
        desc_key = uri + '/_description'

        upsert_desc_query = textwrap.dedent("""
        MERGE (u:Description {key: $desc_key})
        on CREATE SET u={description: $description, key: $desc_key}
        on MATCH SET u={description: $description, key: $desc_key}
        """)

        upsert_desc_tab_relation_query = textwrap.dedent("""
        MATCH (n1:Description {{key: $desc_key}}), (n2:{node_label} {{key: $key}})
        MERGE (n2)-[r2:DESCRIPTION]->(n1)
        RETURN n1.key, n2.key
        """.format(node_label=resource_type.name))

        start = time.time()

        try:
            tx = self._driver.session().begin_transaction()

            tx.run(upsert_desc_query, {'description': description,
                                       'desc_key': desc_key})

            result = tx.run(upsert_desc_tab_relation_query, {'desc_key': desc_key,
                                                             'key': uri})

            if not result.single():
                raise RuntimeError('Failed to update the resource {uri} description'.format(uri=uri))

            # end neo4j transaction
            tx.commit()

        except Exception as e:
            LOGGER.exception('Failed to execute update process')
            if not tx.closed():
                tx.rollback()

            # propagate exception back to api
            raise e

        finally:
            tx.close()
            if LOGGER.isEnabledFor(logging.DEBUG):
                LOGGER.debug('Update process elapsed for {} seconds'.format(time.time() - start))

    @timer_with_counter
    def put_table_description(self, *,
                              table_uri: str,
                              description: str) -> None:
        """
        Update table description with one from user
        :param table_uri: Table uri (key in Neo4j)
        :param description: new value for table description
        """

        self._put_resource_description(resource_type=ResourceType.Table,
                                       uri=table_uri,
                                       description=description)

    @timer_with_counter
    def get_column_description(self, *,
                               table_uri: str,
                               column_name: str) -> Union[str, None]:
        """
        Get the column description based on table uri. Any exception will propagate back to api server.

        :param table_uri:
        :param column_name:
        :return:
        """
        column_description_query = textwrap.dedent("""
        MATCH (tbl:Table {key: $tbl_key})-[:COLUMN]->(c:Column {name: $column_name})-[:DESCRIPTION]->(d:Description)
        RETURN d.description AS description;
        """)

        result = self._execute_cypher_query(statement=column_description_query,
                                            param_dict={'tbl_key': table_uri, 'column_name': column_name})

        column_descrpt = result.single()

        column_description = column_descrpt['description'] if column_descrpt else None

        return column_description

    @timer_with_counter
    def put_column_description(self, *,
                               table_uri: str,
                               column_name: str,
                               description: str) -> None:
        """
        Update column description with input from user
        :param table_uri:
        :param column_name:
        :param description:
        :return:
        """

        column_uri = table_uri + '/' + column_name  # type: str
        desc_key = column_uri + '/_description'

        upsert_desc_query = textwrap.dedent("""
            MERGE (u:Description {key: $desc_key})
            on CREATE SET u={description: $description, key: $desc_key}
            on MATCH SET u={description: $description, key: $desc_key}
            """)

        upsert_desc_col_relation_query = textwrap.dedent("""
            MATCH (n1:Description {key: $desc_key}), (n2:Column {key: $column_key})
            MERGE (n2)-[r2:DESCRIPTION]->(n1)
            RETURN n1.key, n2.key
            """)

        start = time.time()

        try:
            tx = self._driver.session().begin_transaction()

            tx.run(upsert_desc_query, {'description': description,
                                       'desc_key': desc_key})

            result = tx.run(upsert_desc_col_relation_query, {'desc_key': desc_key,
                                                             'column_key': column_uri})

            if not result.single():
                raise RuntimeError('Failed to update the table {tbl} '
                                   'column {col} description'.format(tbl=table_uri,
                                                                     col=column_uri))

            # end neo4j transaction
            tx.commit()

        except Exception as e:

            LOGGER.exception('Failed to execute update process')

            if not tx.closed():
                tx.rollback()

            # propagate error to api
            raise e

        finally:

            tx.close()

            if LOGGER.isEnabledFor(logging.DEBUG):
                LOGGER.debug('Update process elapsed for {} seconds'.format(time.time() - start))

    @timer_with_counter
    def add_owner(self, *,
                  table_uri: str,
                  owner: str) -> None:
        """
        Update table owner informations.
        1. Do a create if not exists query of the owner(user) node.
        2. Do a upsert of the owner/owned_by relation.

        :param table_uri:
        :param owner:
        :return:
        """
        create_owner_query = textwrap.dedent("""
        MERGE (u:User {key: $user_email})
        on CREATE SET u={email: $user_email, key: $user_email}
        """)

        upsert_owner_relation_query = textwrap.dedent("""
        MATCH (n1:User {key: $user_email}), (n2:Table {key: $tbl_key})
        MERGE (n2)-[r2:OWNER]->(n1)
        RETURN n1.key, n2.key
        """)

        try:
            tx = self._driver.session().begin_transaction()
            # upsert the node
            tx.run(create_owner_query, {'user_email': owner})
            result = tx.run(upsert_owner_relation_query, {'user_email': owner,
                                                          'tbl_key': table_uri})

            if not result.single():
                raise RuntimeError('Failed to create relation between '
                                   'owner {owner} and table {tbl}'.format(owner=owner,
                                                                          tbl=table_uri))
        except Exception as e:
            if not tx.closed():
                tx.rollback()
            # propagate the exception back to api
            raise e
        finally:
            tx.commit()
            tx.close()

    @timer_with_counter
    def delete_owner(self, *,
                     table_uri: str,
                     owner: str) -> None:
        """
        Delete the owner / owned_by relationship.

        :param table_uri:
        :param owner:
        :return:
        """
        delete_query = textwrap.dedent("""
        MATCH (n1:User{key: $user_email})<-[r1:OWNER]-(n2:Table {key: $tbl_key}) DELETE r1
        """)

        try:
            tx = self._driver.session().begin_transaction()
            tx.run(delete_query, {'user_email': owner,
                                  'tbl_key': table_uri})
        except Exception as e:
            # propagate the exception back to api
            if not tx.closed():
                tx.rollback()
            raise e
        finally:
            tx.commit()
            tx.close()

    @timer_with_counter
    def add_tag(self, *,
                id: str,
                tag: str,
                tag_type: str = 'default',
                resource_type: ResourceType = ResourceType.Table) -> None:
        """
        Add new tag
        1. Create the node with type Tag if the node doesn't exist.
        2. Create the relation between tag and table if the relation doesn't exist.

        :param id:
        :param tag:
        :param tag_type:
        :param resource_type:
        :return: None
        """
        LOGGER.info('New tag {} for id {} with type {} and resource type {}'.format(tag, id, tag_type,
                                                                                    resource_type.name))

        validation_query = \
            'MATCH (n:{resource_type} {{key: $key}}) return n'.format(resource_type=resource_type.name)

        upsert_tag_query = textwrap.dedent("""
        MERGE (u:Tag {key: $tag})
        on CREATE SET u={tag_type: $tag_type, key: $tag}
        on MATCH SET u={tag_type: $tag_type, key: $tag}
        """)

        upsert_tag_relation_query = textwrap.dedent("""
        MATCH (n1:Tag {{key: $tag, tag_type: $tag_type}}), (n2:{resource_type} {{key: $key}})
        MERGE (n1)-[r1:TAG]->(n2)-[r2:TAGGED_BY]->(n1)
        RETURN n1.key, n2.key
        """.format(resource_type=resource_type.name))

        try:
            tx = self._driver.session().begin_transaction()
            tbl_result = tx.run(validation_query, {'key': id})
            if not tbl_result.single():
                raise NotFoundException('id {} does not exist'.format(id))

            # upsert the node. Currently the type for all the tags is default. We could change it later per UI.
            tx.run(upsert_tag_query, {'tag': tag,
                                      'tag_type': tag_type})
            result = tx.run(upsert_tag_relation_query, {'tag': tag,
                                                        'key': id,
                                                        'tag_type': tag_type})
            if not result.single():
                raise RuntimeError('Failed to create relation between '
                                   'tag {tag} and resource {resource} of resource type: {resource_type}'
                                   .format(tag=tag,
                                           resource=id,
                                           resource_type=resource_type.name))
            tx.commit()
        except Exception as e:
            if not tx.closed():
                tx.rollback()
            # propagate the exception back to api
            raise e
        finally:
            if not tx.closed():
                tx.close()

    @timer_with_counter
    def delete_tag(self, *,
                   id: str,
                   tag: str,
                   tag_type: str = 'default',
                   resource_type: ResourceType = ResourceType.Table) -> None:
        """
        Deletes tag
        1. Delete the relation between resource and the tag
        2. todo(Tao): need to think about whether we should delete the tag if it is an orphan tag.

        :param id:
        :param tag:
        :param tag_type: {default-> normal tag, badge->non writable tag from UI}
        :param resource_type:
        :return:
        """

        LOGGER.info('Delete tag {} for id {} with type {} and resource type: {}'.format(tag, id,
                                                                                        tag_type, resource_type.name))
        delete_query = textwrap.dedent("""
        MATCH (n1:Tag{{key: $tag, tag_type: $tag_type}})-
        [r1:TAG]->(n2:{resource_type} {{key: $key}})-[r2:TAGGED_BY]->(n1) DELETE r1,r2
        """.format(resource_type=resource_type.name))

        try:
            tx = self._driver.session().begin_transaction()
            tx.run(delete_query, {'tag': tag,
                                  'key': id,
                                  'tag_type': tag_type})
        except Exception as e:
            # propagate the exception back to api
            if not tx.closed():
                tx.rollback()
            raise e
        finally:
            tx.commit()
            tx.close()

    @timer_with_counter
    def get_tags(self) -> List:
        """
        Get all existing tags from neo4j

        :return:
        """
        LOGGER.info('Get all the tags')
        # todo: Currently all the tags are default type, we could open it up if we want to include badge
        query = textwrap.dedent("""
        MATCH (t:Tag{tag_type: 'default'})
        OPTIONAL MATCH (tbl:Table)-[:TAGGED_BY]->(t)
        RETURN t as tag_name, count(distinct tbl.key) as tag_count
        """)

        records = self._execute_cypher_query(statement=query,
                                             param_dict={})
        results = []
        for record in records:
            results.append(TagDetail(tag_name=record['tag_name']['key'],
                                     tag_count=record['tag_count']))
        return results

    @timer_with_counter
    def get_latest_updated_ts(self) -> Optional[int]:
        """
        API method to fetch last updated / index timestamp for neo4j, es

        :return:
        """
        query = textwrap.dedent("""
        MATCH (n:Updatedtimestamp{key: 'amundsen_updated_timestamp'}) RETURN n as ts
        """)
        record = self._execute_cypher_query(statement=query,
                                            param_dict={})
        # None means we don't have record for neo4j, es last updated / index ts
        record = record.single()
        if record:
            return record.get('ts', {}).get('latest_timestmap', 0)
        else:
            return None

    @timer_with_counter
    @_CACHE.cache('_get_popular_tables_uris', _GET_POPULAR_TABLE_CACHE_EXPIRY_SEC)
    def _get_popular_tables_uris(self, num_entries: int) -> List[str]:
        """
        Retrieve popular table uris. Will provide tables with top x popularity score.
        Popularity score = number of distinct readers * log(total number of reads)
        The result of this method will be cached based on the key (num_entries), and the cache will be expired based on
        _GET_POPULAR_TABLE_CACHE_EXPIRY_SEC

        For score computation, it uses logarithm on total number of reads so that score won't be affected by small
        number of users reading a lot of times.
        :return: Iterable of table uri
        """
        query = textwrap.dedent("""
        MATCH (tbl:Table)-[r:READ_BY]->(u:User)
        WITH tbl.key as table_key, count(distinct u) as readers, sum(r.read_count) as total_reads
        WHERE readers > 10
        RETURN table_key, readers, total_reads, (readers * log(total_reads)) as score
        ORDER BY score DESC LIMIT $num_entries;
        """)

        LOGGER.info('Querying popular tables URIs')
        records = self._execute_cypher_query(statement=query,
                                             param_dict={'num_entries': num_entries})

        return [record['table_key'] for record in records]

    @timer_with_counter
    def get_popular_tables(self, *, num_entries: int) -> List[PopularTable]:
        """
        Retrieve popular tables. As popular table computation requires full scan of table and user relationship,
        it will utilize cached method _get_popular_tables_uris.

        :param num_entries:
        :return: Iterable of PopularTable
        """

        table_uris = self._get_popular_tables_uris(num_entries)
        if not table_uris:
            return []

        query = textwrap.dedent("""
        MATCH (db:Database)-[:CLUSTER]->(clstr:Cluster)-[:SCHEMA]->(schema:Schema)-[:TABLE]->(tbl:Table)
        WHERE tbl.key IN $table_uris
        WITH db.name as database_name, clstr.name as cluster_name, schema.name as schema_name, tbl
        OPTIONAL MATCH (tbl)-[:DESCRIPTION]->(dscrpt:Description)
        RETURN database_name, cluster_name, schema_name, tbl.name as table_name,
        dscrpt.description as table_description;
        """)

        records = self._execute_cypher_query(statement=query,
                                             param_dict={'table_uris': table_uris})

        popular_tables = []
        for record in records:
            popular_table = PopularTable(database=record['database_name'],
                                         cluster=record['cluster_name'],
                                         schema=record['schema_name'],
                                         name=record['table_name'],
                                         description=self._safe_get(record, 'table_description'))
            popular_tables.append(popular_table)
        return popular_tables

    @timer_with_counter
    def get_user(self, *, id: str) -> Union[UserEntity, None]:
        """
        Retrieve user detail based on user_id(email).

        :param user_id: the email for the given user
        :return:
        """

        query = textwrap.dedent("""
        MATCH (user:User {key: $user_id})
        OPTIONAL MATCH (user)-[:MANAGE_BY]->(manager:User)
        RETURN user as user_record, manager as manager_record
        """)

        record = self._execute_cypher_query(statement=query,
                                            param_dict={'user_id': id})
        single_result = record.single()

        if not single_result:
            raise NotFoundException('User {user_id} '
                                    'not found in the graph'.format(user_id=id))

        record = single_result.get('user_record', {})
        manager_record = single_result.get('manager_record', {})
        if manager_record:
            manager_name = manager_record.get('full_name', '')
        else:
            manager_name = ''

        return self._build_user_from_record(record=record, manager_name=manager_name)

    def get_users(self) -> List[UserEntity]:
        statement = "MATCH (usr:User) WHERE usr.is_active = true RETURN collect(usr) as users"

        record = self._execute_cypher_query(statement=statement, param_dict={})
        result = record.single()
        if not result or not result.get('users'):
            raise NotFoundException('Error getting users')

        return [self._build_user_from_record(record=rec) for rec in result['users']]

    @staticmethod
    def _build_user_from_record(record: dict, manager_name: str = '') -> UserEntity:
        return UserEntity(email=record['email'],
                          first_name=record.get('first_name'),
                          last_name=record.get('last_name'),
                          full_name=record.get('full_name'),
                          is_active=record.get('is_active', False),
                          github_username=record.get('github_username'),
                          team_name=record.get('team_name'),
                          slack_id=record.get('slack_id'),
                          employee_type=record.get('employee_type'),
                          role_name=record.get('role_name'),
                          manager_fullname=manager_name)

    @staticmethod
    def _get_user_table_relationship_clause(relation_type: UserResourceRel, tbl_key: str = None,
                                            user_key: str = None) -> str:
        """
        Returns the relationship clause of a cypher query between users and tables
        The User node is 'usr', the table node is 'tbl', and the relationship is 'rel'
        e.g. (usr:User)-[rel:READ]->(tbl:Table), (usr)-[rel:READ]->(tbl)
        """
        tbl_matcher: str = ''
        user_matcher: str = ''

        if tbl_key is not None:
            tbl_matcher += ':Table'
            if tbl_key != '':
                tbl_matcher += f' {{key: "{tbl_key}"}}'

        if user_key is not None:
            user_matcher += ':User'
            if user_key != '':
                user_matcher += f' {{key: "{user_key}"}}'

        if relation_type == UserResourceRel.follow:
            relation = f'(usr{user_matcher})-[rel:FOLLOW]->(tbl{tbl_matcher})'
        elif relation_type == UserResourceRel.own:
            relation = f'(usr{user_matcher})<-[rel:OWNER]-(tbl{tbl_matcher})'
        elif relation_type == UserResourceRel.read:
            relation = f'(usr{user_matcher})-[rel:READ]->(tbl{tbl_matcher})'
        else:
            raise NotImplementedError(f'The relation type {relation_type} is not defined!')
        return relation

    @timer_with_counter
    def get_table_by_user_relation(self, *, user_email: str, relation_type: UserResourceRel) -> Dict[str, Any]:
        """
        Retrive all follow the resources per user based on the relation.
        We start with table resources only, then add dashboard.

        :param user_email: the email of the user
        :param relation_type: the relation between the user and the resource
        :return:
        """
        rel_clause: str = self._get_user_table_relationship_clause(relation_type=relation_type,
                                                                   tbl_key='',
                                                                   user_key=user_email)
        query = textwrap.dedent(f"""
        MATCH {rel_clause}<-[:TABLE]-(schema:Schema)<-[:SCHEMA]-(clstr:Cluster)<-[:CLUSTER]-(db:Database)
        WITH db, clstr, schema, tbl
        OPTIONAL MATCH (tbl)-[:DESCRIPTION]->(tbl_dscrpt:Description)
        RETURN db, clstr, schema, tbl, tbl_dscrpt""")

        table_records = self._execute_cypher_query(statement=query, param_dict={'query_key': user_email})

        if not table_records:
            raise NotFoundException('User {user_id} does not {relation} any resources'.format(user_id=user_email,
                                                                                              relation=relation_type))
        results = []
        for record in table_records:
            results.append(PopularTable(
                database=record['db']['name'],
                cluster=record['clstr']['name'],
                schema=record['schema']['name'],
                name=record['tbl']['name'],
                description=self._safe_get(record, 'tbl_dscrpt', 'description')))
        return {'table': results}

    @timer_with_counter
    def get_frequently_used_tables(self, *, user_email: str) -> Dict[str, Any]:
        """
        Retrieves all Table the resources per user on READ relation.

        :param user_email: the email of the user
        :return:
        """

        query = textwrap.dedent("""
        MATCH (user:User {key: $query_key})-[r:READ]->(tbl:Table)
        WHERE EXISTS(r.published_tag) AND r.published_tag IS NOT NULL
        WITH user, r, tbl ORDER BY r.published_tag DESC, r.read_count DESC LIMIT 50
        MATCH (tbl:Table)<-[:TABLE]-(schema:Schema)<-[:SCHEMA]-(clstr:Cluster)<-[:CLUSTER]-(db:Database)
        OPTIONAL MATCH (tbl)-[:DESCRIPTION]->(tbl_dscrpt:Description)
        RETURN db, clstr, schema, tbl, tbl_dscrpt
        """)

        table_records = self._execute_cypher_query(statement=query, param_dict={'query_key': user_email})

        if not table_records:
            raise NotFoundException('User {user_id} does not READ any resources'.format(user_id=user_email))
        results = []

        for record in table_records:
            results.append(PopularTable(
                database=record['db']['name'],
                cluster=record['clstr']['name'],
                schema=record['schema']['name'],
                name=record['tbl']['name'],
                description=self._safe_get(record, 'tbl_dscrpt', 'description')))
        return {'table': results}

    @timer_with_counter
    def add_table_relation_by_user(self, *,
                                   table_uri: str,
                                   user_email: str,
                                   relation_type: UserResourceRel) -> None:
        """
        Update table user informations.
        1. Do a upsert of the user node.
        2. Do a upsert of the relation/reverse-relation edge.

        :param table_uri:
        :param user_email:
        :param relation_type:
        :return:
        """

        upsert_user_query = textwrap.dedent("""
        MERGE (u:User {key: $user_email})
        on CREATE SET u={email: $user_email, key: $user_email}
        """)

        user_email_clause = f'key: "{user_email}"'
        tbl_key = f'key: "{table_uri}"'

        rel_clause: str = self._get_user_table_relationship_clause(relation_type=relation_type)
        upsert_user_relation_query = textwrap.dedent(f"""
        MATCH (usr:User {{{user_email_clause}}}), (tbl:Table {{{tbl_key}}})
        MERGE {rel_clause}
        RETURN usr.key, tbl.key
        """)

        try:
            tx = self._driver.session().begin_transaction()
            # upsert the node
            tx.run(upsert_user_query, {'user_email': user_email})
            result = tx.run(upsert_user_relation_query, {})

            if not result.single():
                raise RuntimeError('Failed to create relation between '
                                   'user {user} and table {tbl}'.format(user=user_email,
                                                                        tbl=table_uri))
            tx.commit()
        except Exception as e:
            if not tx.closed():
                tx.rollback()
            # propagate the exception back to api
            raise e
        finally:
            tx.close()

    @timer_with_counter
    def delete_table_relation_by_user(self, *,
                                      table_uri: str,
                                      user_email: str,
                                      relation_type: UserResourceRel) -> None:
        """
        Delete the relationship between user and resources.

        :param table_uri:
        :param user_email:
        :param relation_type:
        :return:
        """
        rel_clause: str = self._get_user_table_relationship_clause(relation_type=relation_type,
                                                                   user_key=user_email,
                                                                   tbl_key=table_uri)

        delete_query = textwrap.dedent(f"""
        MATCH {rel_clause}
        DELETE rel
        """)

        try:
            tx = self._driver.session().begin_transaction()
            tx.run(delete_query, {})
            tx.commit()
        except Exception as e:
            # propagate the exception back to api
            if not tx.closed():
                tx.rollback()
            raise e
        finally:
            tx.close()

    @timer_with_counter
    def get_dashboard(self,
                      id: str,
                      ) -> DashboardDetailEntity:

        get_dashboard_detail_query = textwrap.dedent(u"""
        MATCH (d:Dashboard {key: $query_key})-[:DASHBOARD_OF]->(dg:Dashboardgroup)-[:DASHBOARD_GROUP_OF]->(c:Cluster)
        OPTIONAL MATCH (d)-[:DESCRIPTION]->(description:Description)
        OPTIONAL MATCH (d)-[:EXECUTED]->(last_exec:Execution) WHERE split(last_exec.key, '/')[5] = '_last_execution'
        OPTIONAL MATCH (d)-[:LAST_UPDATED_AT]->(t:Timestamp)
        OPTIONAL MATCH (d)-[:OWNER]->(owner:User)
        OPTIONAL MATCH (d)-[:TAG]->(tag:Tag)
        RETURN
        c.name as cluster_name,
        d.key as uri,
        d.dashboard_url as url,
        d.name as name,
        toInteger(d.created_timestamp) as created_timestamp,
        description.description as description,
        dg.name as group_name,
        dg.dashboard_group_url as group_url,
        toInteger(last_exec.timestamp) as last_run_timestamp,
        last_exec.state as last_run_state,
        toInteger(t.timestamp) as updated_timestamp,
        collect(owner) as owners,
        collect(tag) as tags;
        """
                                                     )
        record = self._execute_cypher_query(statement=get_dashboard_detail_query,
                                            param_dict={'query_key': id}).single()

        if not record:
            raise NotFoundException('No dashboard exist with URI: {}'.format(id))

        owners = [self._build_user_from_record(record=owner) for owner in record['owners']]
        tags = [Tag(tag_type=tag['tag_type'], tag_name=tag['key']) for tag in record['tags']]

        return DashboardDetailEntity(uri=record['uri'],
                                     cluster=record['cluster_name'],
                                     url=record['url'],
                                     name=record['name'],
                                     created_timestamp=record['created_timestamp'],
                                     description=self._safe_get(record, 'description'),
                                     group_name=self._safe_get(record, 'group_name'),
                                     group_url=self._safe_get(record, 'group_url'),
                                     last_run_timestamp=self._safe_get(record, 'last_run_timestamp'),
                                     last_run_state=self._safe_get(record, 'last_run_state'),
                                     updated_timestamp=self._safe_get(record, 'updated_timestamp'),
                                     owners=owners,
                                     tags=tags)

    @timer_with_counter
    def get_dashboard_description(self, *,
                                  id: str) -> Description:
        """
        Get the dashboard description based on dashboard uri. Any exception will propagate back to api server.

        :param id:
        :return:
        """

        return self._get_resource_description(resource_type=ResourceType.Dashboard, uri=id)

    @timer_with_counter
    def put_dashboard_description(self, *,
                                  id: str,
                                  description: str) -> None:
        """
        Update Dashboard description
        :param id: Dashboard URI
        :param description: new value for Dashboard description
        """

        self._put_resource_description(resource_type=ResourceType.Dashboard,
                                       uri=id,
                                       description=description)
