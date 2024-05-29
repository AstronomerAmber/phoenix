from datetime import datetime
from enum import Enum

import strawberry
from strawberry.relay import Node, NodeID

from phoenix.server.api.types.ExampleRevisionInterface import ExampleRevision


@strawberry.enum
class RevisionKind(Enum):
    CREATE = "CREATE"
    PATCH = "PATCH"
    DELETE = "DELETE"


@strawberry.type
class DatasetExampleRevision(Node, ExampleRevision):
    """
    Represents a revision (i.e., update or alteration) of a dataset example.
    """

    id_attr: NodeID[int]
    revision_kind: RevisionKind
    created_at: datetime
