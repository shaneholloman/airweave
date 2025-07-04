"""DAG service."""

from collections import Counter
from typing import Dict, List, Set, Tuple, Type
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from airweave import crud, schemas
from airweave.db.unit_of_work import UnitOfWork
from airweave.platform.entities._base import (
    BaseEntity,
    ChunkEntity,
    FileEntity,
    ParentEntity,
    WebEntity,
)
from airweave.platform.entities.web import WebFileEntity
from airweave.platform.locator import resource_locator
from airweave.schemas.auth import AuthContext
from airweave.schemas.dag import DagEdgeCreate, DagNodeCreate, NodeType, SyncDagCreate


class DagService:
    """DAG service."""

    async def create_initial_dag(
        self,
        db: AsyncSession,
        *,
        sync_id: UUID,
        auth_context: AuthContext,
        uow: UnitOfWork,
    ) -> schemas.SyncDag:
        """Create an initial DAG with source, entities, and destination.

        Args:
        ----
            db (AsyncSession): The database session.
            sync_id (UUID): The ID of the sync to create the DAG for.
            auth_context (AuthContext): The authentication context.
            uow (UnitOfWork): The unit of work.

        Returns:
        -------
            schemas.SyncDag: The created DAG.
        """
        # Get sync and validate
        sync = await self._get_and_validate_sync(db, sync_id, auth_context)

        # Get file chunker transformer
        file_chunker = await self._get_file_chunker(db)

        # Get web fetcher transformer
        web_fetcher = await self._get_web_fetcher(db)

        # Get source connection and entity definitions
        (
            source,
            source_connection,
            entity_definitions,
        ) = await self._get_source_and_entity_definitions(db, sync, auth_context)

        # Get or create destinations
        (
            destinations,
            destination_connections,
        ) = await self._get_destinations_and_destination_connections(db, sync, auth_context)

        # Initialize DAG components
        nodes: List[DagNodeCreate] = []
        edges: List[DagEdgeCreate] = []
        processed_entity_ids: Set[UUID] = set()

        # Create source and destination nodes
        source_node_id, destination_node_ids = await self._create_source_and_destination_nodes(
            source, source_connection, destinations, destination_connections, nodes
        )

        # Process entity definitions
        await self._process_all_entity_definitions(
            db,
            entity_definitions,
            processed_entity_ids,
            file_chunker,
            web_fetcher,
            source_node_id,
            destination_node_ids,
            nodes,
            edges,
        )

        # Create and return the DAG
        return await self._create_and_save_dag(db, sync, sync_id, nodes, edges, auth_context, uow)

    async def _create_source_and_destination_nodes(
        self, source, source_connection, destinations, destination_connections, nodes
    ) -> Tuple[UUID, List[UUID]]:
        """Create source and destination nodes for the DAG."""
        source_node_id = uuid4()
        destination_node_ids = []

        # Add source node
        nodes.append(
            DagNodeCreate(
                id=source_node_id,
                type=NodeType.source,
                name=source.name,
                connection_id=source_connection.id,
            )
        )

        # Add destination nodes - create a unique ID for each destination
        for destination, destination_connection in zip(
            destinations, destination_connections, strict=True
        ):
            dest_node_id = uuid4()
            destination_node_ids.append(dest_node_id)

            nodes.append(
                DagNodeCreate(
                    id=dest_node_id,
                    type=NodeType.destination,
                    name=destination.name,
                    connection_id=destination_connection.id,
                )
            )

        return source_node_id, destination_node_ids

    async def _process_all_entity_definitions(
        self,
        db,
        entity_definitions,
        processed_entity_ids,
        file_chunker,
        web_fetcher,
        source_node_id,
        destination_node_ids,
        nodes,
        edges,
    ):
        """Process all entity definitions and create corresponding nodes and edges."""
        for entity_definition_id, entity_data in entity_definitions.items():
            if entity_definition_id in processed_entity_ids:
                continue

            processed_entity_ids.add(entity_definition_id)
            entity_class = entity_data["entity_class"]
            entity_definition = entity_data["entity_definition"]

            # Handle file entities
            if issubclass(entity_class, FileEntity):
                destination_node_id = destination_node_ids[0] if destination_node_ids else None
                if not destination_node_id:
                    raise ValueError("No destination node ID available for file entity processing")

                await self._process_file_entity(
                    db=db,
                    entity_class=entity_class,
                    entity_definition=entity_definition,
                    file_chunker=file_chunker,
                    source_node_id=source_node_id,
                    destination_node_id=destination_node_id,
                    nodes=nodes,
                    edges=edges,
                    processed_entity_ids=processed_entity_ids,
                )
            # Handle web entities
            elif issubclass(entity_class, WebEntity):
                destination_node_id = destination_node_ids[0] if destination_node_ids else None
                if not destination_node_id:
                    raise ValueError("No destination node ID available for web entity processing")

                await self._process_web_entity(
                    db=db,
                    entity_class=entity_class,
                    entity_definition=entity_definition,
                    web_fetcher=web_fetcher,
                    file_chunker=file_chunker,
                    source_node_id=source_node_id,
                    destination_node_id=destination_node_id,
                    nodes=nodes,
                    edges=edges,
                    processed_entity_ids=processed_entity_ids,
                )
            # Handle regular entities
            else:
                await self._process_regular_entity(
                    entity_definition_id,
                    entity_definition,
                    source_node_id,
                    destination_node_ids,
                    nodes,
                    edges,
                )

    async def _process_regular_entity(
        self,
        entity_definition_id,
        entity_definition,
        source_node_id,
        destination_node_ids,
        nodes,
        edges,
    ):
        """Process a regular entity and create necessary nodes and edges."""
        entity_node_id = uuid4()
        nodes.append(
            DagNodeCreate(
                id=entity_node_id,
                type=NodeType.entity,
                name=entity_definition.name,
                entity_definition_id=entity_definition_id,
            )
        )

        # Connect source to entity
        edges.append(
            DagEdgeCreate(
                from_node_id=source_node_id,
                to_node_id=entity_node_id,
            )
        )

        # Connect entity to all destination nodes
        for dest_node_id in destination_node_ids:
            edges.append(
                DagEdgeCreate(
                    from_node_id=entity_node_id,
                    to_node_id=dest_node_id,
                )
            )

    async def _create_and_save_dag(
        self, db, sync, sync_id, nodes, edges, auth_context, uow
    ) -> schemas.SyncDag:
        """Create and save the DAG with nodes and edges."""
        sync_dag_create = SyncDagCreate(
            name=f"DAG for {sync.name}",
            sync_id=sync_id,
            nodes=nodes,
            edges=edges,
        )

        try:
            from airweave.core.logging import logger

            logger.info(
                f"Creating DAG for sync {sync.name} with {len(nodes)} nodes and {len(edges)} edges"
            )

            sync_dag = await crud.sync_dag.create_with_nodes_and_edges(
                db, obj_in=sync_dag_create, auth_context=auth_context, uow=uow
            )
            logger.info(f"Successfully created DAG with ID {sync_dag.id}")
            return schemas.SyncDag.model_validate(sync_dag, from_attributes=True)
        except Exception as e:
            from airweave.core.logging import logger

            logger.error(f"Error creating DAG: {e}")

            self._log_dag_errors(nodes, edges)
            raise

    def _log_dag_errors(self, nodes, edges):
        """Log diagnostic information for DAG creation errors."""
        from airweave.core.logging import logger

        logger.error(f"Total nodes: {len(nodes)}, Total edges: {len(edges)}")

        # Check for any duplicate node IDs which could cause issues
        node_ids = [node.id for node in nodes]
        duplicate_ids = {id: count for id, count in Counter(node_ids).items() if count > 1}
        if duplicate_ids:
            logger.error(f"Found duplicate node IDs: {duplicate_ids}")

        # Log edge relationships to verify they point to valid nodes
        edge_relationships = [(edge.from_node_id, edge.to_node_id) for edge in edges]

        # Check for edges referencing non-existent nodes
        node_id_set = set(node_ids)
        invalid_edges = []
        for from_id, to_id in edge_relationships:
            if from_id not in node_id_set:
                invalid_edges.append(f"Edge from_node_id {from_id} not in nodes")
            if to_id not in node_id_set:
                invalid_edges.append(f"Edge to_node_id {to_id} not in nodes")

        if invalid_edges:
            logger.error(f"Found invalid edges: {invalid_edges}")

    async def _get_and_validate_sync(
        self, db: AsyncSession, sync_id: UUID, auth_context: AuthContext
    ) -> schemas.Sync:
        """Get and validate that the sync exists.

        Args:
        ----
            db (AsyncSession): The database session.
            sync_id (UUID): The ID of the sync to get.
            auth_context (AuthContext): The authentication context.

        Returns:
        -------
            schemas.Sync: The sync.

        Raises:
        ------
            Exception: If the sync is not found.
        """
        sync = await crud.sync.get(db, id=sync_id, auth_context=auth_context, with_connections=True)
        if not sync:
            raise Exception(f"Sync for {sync_id} not found")
        return sync

    async def _get_file_chunker(self, db: AsyncSession) -> schemas.Transformer:
        """Get the file chunker transformer."""
        transformers = await crud.transformer.get_all(db)
        file_chunker = next(
            (t for t in transformers if t.method_name == "file_chunker"),
            None,
        )
        if not file_chunker:
            raise Exception("No file chunker found")
        return file_chunker

    async def _get_web_fetcher(self, db: AsyncSession) -> schemas.Transformer:
        """Get the web fetcher transformer."""
        transformers = await crud.transformer.get_all(db)
        web_fetcher = next(
            (t for t in transformers if t.method_name == "web_fetcher"),
            None,
        )
        if not web_fetcher:
            raise Exception("No web fetcher found")
        return web_fetcher

    async def _get_source_and_entity_definitions(
        self, db: AsyncSession, sync: schemas.Sync, auth_context: AuthContext
    ) -> Tuple[schemas.Source, schemas.Connection, Dict]:
        """Get source connection and entity definitions."""
        source_connection = await crud.connection.get(
            db, id=sync.source_connection_id, auth_context=auth_context
        )
        if not source_connection:
            raise Exception(f"Source connection for {sync.source_connection_id} not found")

        source = await crud.source.get_by_short_name(db, short_name=source_connection.short_name)

        entity_definitions = await crud.entity_definition.get_multi_by_ids(
            db, ids=source.output_entity_definition_ids
        )

        entity_definitions_dict = {
            entity_definition.id: {
                "entity_class": resource_locator.get_entity_definition(entity_definition),
                "entity_definition": entity_definition,
            }
            for entity_definition in entity_definitions
        }

        return source, source_connection, entity_definitions_dict

    async def _get_destinations_and_destination_connections(
        self, db: AsyncSession, sync: schemas.Sync, auth_context: AuthContext
    ) -> Tuple[List[schemas.Destination], List[schemas.Connection]]:
        """Get or create destinations and destination connections.

        Args:
        ----
            db (AsyncSession): The database session.
            sync (schemas.Sync): The sync to get the destinations and destination connections for.
            auth_context (AuthContext): The authentication context.

        Returns:
        -------
            Tuple[List[schemas.Destination], List[schemas.Connection]]: The destinations and
                destination connections.

        Raises:
        ------
            HTTPException: If a destination or destination connection is not found.
        """
        # Initialize lists
        destinations = []
        destination_connections = []

        # Get destinations and destination connections
        if sync.destination_connection_ids:
            for destination_connection_id in sync.destination_connection_ids:
                destination_connection = await crud.connection.get(
                    db, id=destination_connection_id, auth_context=auth_context
                )
                if not destination_connection:
                    raise HTTPException(status_code=404, detail="Destination connection not found")
                destination_connections.append(destination_connection)

                destination = await crud.destination.get_by_short_name(
                    db, short_name=destination_connection.short_name
                )
                if not destination:
                    raise HTTPException(status_code=404, detail="Destination not found")
                destinations.append(destination)
        return destinations, destination_connections

    async def _process_file_entity(
        self,
        db: AsyncSession,
        entity_class: Type[FileEntity],
        entity_definition: schemas.EntityDefinition,
        file_chunker: schemas.Transformer,
        source_node_id: UUID,
        destination_node_id: UUID,
        nodes: List[DagNodeCreate],
        edges: List[DagEdgeCreate],
        processed_entity_ids: Set[UUID],
    ) -> None:
        """Process a file entity and create necessary nodes and edges."""
        # Get parent and chunk entity classes
        parent_entity_class, chunk_entity_class = entity_class.create_parent_chunk_models()

        # Get entity definitions for parent and chunk
        parent_entity_definition = await self._get_entity_definition_for_entity_class(
            db, parent_entity_class
        )
        chunk_entity_definition = await self._get_entity_definition_for_entity_class(
            db, chunk_entity_class
        )

        # Validate that we have the necessary entity definitions
        if not parent_entity_definition or not chunk_entity_definition:
            from airweave.core.logging import logger

            logger.error(f"Missing entity definitions for {entity_class.__name__}")
            if not parent_entity_definition:
                logger.error(f"Missing parent entity definition for {parent_entity_class.__name__}")
            if not chunk_entity_definition:
                logger.error(f"Missing chunk entity definition for {chunk_entity_class.__name__}")
            raise ValueError(f"Missing entity definitions for {entity_class.__name__}")

        # Mark parent and chunk entity definitions as processed
        processed_entity_ids.add(parent_entity_definition.id)
        processed_entity_ids.add(chunk_entity_definition.id)

        # Create file entity node
        file_node_id = uuid4()
        nodes.append(
            DagNodeCreate(
                id=file_node_id,
                type=NodeType.entity,
                name=entity_definition.name,
                entity_definition_id=entity_definition.id,
            )
        )

        # Create file chunker transformer node
        chunker_node_id = uuid4()
        nodes.append(
            DagNodeCreate(
                id=chunker_node_id,
                type=NodeType.transformer,
                name=file_chunker.name,
                transformer_id=file_chunker.id,
            )
        )

        # Create parent chunk node
        parent_node_id = uuid4()
        nodes.append(
            DagNodeCreate(
                id=parent_node_id,
                type=NodeType.entity,
                name=parent_entity_definition.name,
                entity_definition_id=parent_entity_definition.id,
            )
        )

        # Create chunk entity node
        chunk_node_id = uuid4()
        nodes.append(
            DagNodeCreate(  # Default value
                id=chunk_node_id,
                type=NodeType.entity,
                name=chunk_entity_definition.name,
                entity_definition_id=chunk_entity_definition.id,
            )
        )

        # Create edges
        # Source -> File Entity
        edges.append(DagEdgeCreate(from_node_id=source_node_id, to_node_id=file_node_id))

        # File Entity -> File Chunker
        edges.append(DagEdgeCreate(from_node_id=file_node_id, to_node_id=chunker_node_id))

        # File Chunker -> Parent Entity
        edges.append(DagEdgeCreate(from_node_id=chunker_node_id, to_node_id=parent_node_id))

        # File Chunker -> Chunk Entity
        edges.append(DagEdgeCreate(from_node_id=chunker_node_id, to_node_id=chunk_node_id))

        # Parent Entity -> Destination
        edges.append(DagEdgeCreate(from_node_id=parent_node_id, to_node_id=destination_node_id))

        # Chunk Entity -> Destination
        edges.append(DagEdgeCreate(from_node_id=chunk_node_id, to_node_id=destination_node_id))

    async def _process_web_entity(
        self,
        db: AsyncSession,
        entity_class: Type[WebEntity],
        entity_definition: schemas.EntityDefinition,
        web_fetcher: schemas.Transformer,
        file_chunker: schemas.Transformer,
        source_node_id: UUID,
        destination_node_id: UUID,
        nodes: List[DagNodeCreate],
        edges: List[DagEdgeCreate],
        processed_entity_ids: Set[UUID],
    ) -> None:
        """Process a web entity and create necessary nodes and edges.

        Flow: WebEntity -> web_fetcher -> FileEntity -> file_chunker -> ParentEntity + ChunkEntity
        """
        # For WebEntity, we need to get the FileEntity parent and chunk models
        # since the web fetcher converts WebEntity -> FileEntity
        file_entity_class = WebFileEntity
        parent_entity_class, chunk_entity_class = file_entity_class.create_parent_chunk_models()

        # Get entity definitions for parent and chunk
        parent_entity_definition = await self._get_entity_definition_for_entity_class(
            db, parent_entity_class
        )
        chunk_entity_definition = await self._get_entity_definition_for_entity_class(
            db, chunk_entity_class
        )

        # Get FileEntity definition for the intermediate node
        file_entity_definition = await self._get_entity_definition_for_entity_class(
            db, file_entity_class
        )

        # Validate that we have the necessary entity definitions
        if (
            not parent_entity_definition
            or not chunk_entity_definition
            or not file_entity_definition
        ):
            from airweave.core.logging import logger

            logger.error("Missing entity definitions for FileEntity parent/chunk models")
            if not parent_entity_definition:
                logger.error(f"Missing parent entity definition for {parent_entity_class.__name__}")
            if not chunk_entity_definition:
                logger.error(f"Missing chunk entity definition for {chunk_entity_class.__name__}")
            if not file_entity_definition:
                logger.error(f"Missing file entity definition for {file_entity_class.__name__}")
            raise ValueError("Missing entity definitions for FileEntity parent/chunk models")

        # Mark parent and chunk entity definitions as processed
        processed_entity_ids.add(parent_entity_definition.id)
        processed_entity_ids.add(chunk_entity_definition.id)
        processed_entity_ids.add(file_entity_definition.id)

        # Create web entity node
        web_node_id = uuid4()
        nodes.append(
            DagNodeCreate(
                id=web_node_id,
                type=NodeType.entity,
                name=entity_definition.name,
                entity_definition_id=entity_definition.id,
            )
        )

        # Create web fetcher transformer node
        fetcher_node_id = uuid4()
        nodes.append(
            DagNodeCreate(
                id=fetcher_node_id,
                type=NodeType.transformer,
                name=web_fetcher.name,
                transformer_id=web_fetcher.id,
            )
        )

        # Create intermediate FileEntity node (output of web_fetcher)
        file_entity_node_id = uuid4()
        nodes.append(
            DagNodeCreate(
                id=file_entity_node_id,
                type=NodeType.entity,
                name=file_entity_definition.name,
                entity_definition_id=file_entity_definition.id,
            )
        )

        # Create file chunker transformer node
        chunker_node_id = uuid4()
        nodes.append(
            DagNodeCreate(
                id=chunker_node_id,
                type=NodeType.transformer,
                name=file_chunker.name,
                transformer_id=file_chunker.id,
            )
        )

        # Create parent chunk node
        parent_node_id = uuid4()
        nodes.append(
            DagNodeCreate(
                id=parent_node_id,
                type=NodeType.entity,
                name=parent_entity_definition.name,
                entity_definition_id=parent_entity_definition.id,
            )
        )

        # Create chunk entity node
        chunk_node_id = uuid4()
        nodes.append(
            DagNodeCreate(
                id=chunk_node_id,
                type=NodeType.entity,
                name=chunk_entity_definition.name,
                entity_definition_id=chunk_entity_definition.id,
            )
        )

        # Create edges
        # Source -> Web Entity
        edges.append(DagEdgeCreate(from_node_id=source_node_id, to_node_id=web_node_id))

        # Web Entity -> Web Fetcher
        edges.append(DagEdgeCreate(from_node_id=web_node_id, to_node_id=fetcher_node_id))

        # Web Fetcher -> FileEntity
        edges.append(DagEdgeCreate(from_node_id=fetcher_node_id, to_node_id=file_entity_node_id))

        # FileEntity -> File Chunker
        edges.append(DagEdgeCreate(from_node_id=file_entity_node_id, to_node_id=chunker_node_id))

        # File Chunker -> Parent Entity
        edges.append(DagEdgeCreate(from_node_id=chunker_node_id, to_node_id=parent_node_id))

        # File Chunker -> Chunk Entity
        edges.append(DagEdgeCreate(from_node_id=chunker_node_id, to_node_id=chunk_node_id))

        # Parent Entity -> Destination
        edges.append(DagEdgeCreate(from_node_id=parent_node_id, to_node_id=destination_node_id))

        # Chunk Entity -> Destination
        edges.append(DagEdgeCreate(from_node_id=chunk_node_id, to_node_id=destination_node_id))

    async def _get_parent_and_chunk_entity_classes_for_entity_definition(
        self, entity_definition: schemas.EntityDefinition
    ) -> tuple[Type[ParentEntity], Type[ChunkEntity]]:
        """Get the parent and chunk entity classes for a given entity definition.

        Args:
        ----
            entity_definition (schemas.EntityDefinition): The entity definition to get the parent
                and chunk entity classes for.

        Returns:
        -------
            tuple[Type[ParentEntity], Type[ChunkEntity]]: The parent and chunk entity classes.
        """
        entity_class = resource_locator.get_entity_definition(entity_definition)
        return entity_class.create_parent_chunk_models()

    async def _get_entity_definition_for_entity_class(
        self, db: AsyncSession, entity_class: Type[BaseEntity]
    ) -> schemas.EntityDefinition:
        """Get the entity definition for a given entity class.

        Args:
        ----
            db (AsyncSession): The database session.
            entity_class (Type[BaseEntity]): The entity class to get the entity definition for.

        Returns:
        -------
            schemas.EntityDefinition: The entity definition.
        """
        entity_definition = await crud.entity_definition.get_by_entity_class_name(
            db, entity_class_name=entity_class.__name__
        )
        return entity_definition


dag_service = DagService()
