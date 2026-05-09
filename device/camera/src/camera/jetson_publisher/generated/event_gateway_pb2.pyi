import datetime

from google.protobuf import struct_pb2 as _struct_pb2
from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class EventEnvelope(_message.Message):
    __slots__ = ("event_type", "resource_id", "source", "occurred_at", "idempotency_key", "payload")
    EVENT_TYPE_FIELD_NUMBER: _ClassVar[int]
    RESOURCE_ID_FIELD_NUMBER: _ClassVar[int]
    SOURCE_FIELD_NUMBER: _ClassVar[int]
    OCCURRED_AT_FIELD_NUMBER: _ClassVar[int]
    IDEMPOTENCY_KEY_FIELD_NUMBER: _ClassVar[int]
    PAYLOAD_FIELD_NUMBER: _ClassVar[int]
    event_type: str
    resource_id: str
    source: str
    occurred_at: _timestamp_pb2.Timestamp
    idempotency_key: str
    payload: _struct_pb2.Struct
    def __init__(self, event_type: _Optional[str] = ..., resource_id: _Optional[str] = ..., source: _Optional[str] = ..., occurred_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., idempotency_key: _Optional[str] = ..., payload: _Optional[_Union[_struct_pb2.Struct, _Mapping]] = ...) -> None: ...

class PublishEventRequest(_message.Message):
    __slots__ = ("event",)
    EVENT_FIELD_NUMBER: _ClassVar[int]
    event: EventEnvelope
    def __init__(self, event: _Optional[_Union[EventEnvelope, _Mapping]] = ...) -> None: ...

class PublishEventResponse(_message.Message):
    __slots__ = ("accepted", "reason", "deduplicated")
    ACCEPTED_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    DEDUPLICATED_FIELD_NUMBER: _ClassVar[int]
    accepted: bool
    reason: str
    deduplicated: bool
    def __init__(self, accepted: bool = ..., reason: _Optional[str] = ..., deduplicated: bool = ...) -> None: ...

class WatchEventsRequest(_message.Message):
    __slots__ = ("event_types", "consumer")
    EVENT_TYPES_FIELD_NUMBER: _ClassVar[int]
    CONSUMER_FIELD_NUMBER: _ClassVar[int]
    event_types: _containers.RepeatedScalarFieldContainer[str]
    consumer: str
    def __init__(self, event_types: _Optional[_Iterable[str]] = ..., consumer: _Optional[str] = ...) -> None: ...
