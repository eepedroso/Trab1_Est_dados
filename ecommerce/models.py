"""Layouts de tamanho fixo: números binários e textos UTF-8 com espaços."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import struct
from typing import Generic, TypeVar

UINT64_MAX = 2**64 - 1
INT64_MAX = 2**63 - 1
EVENT_TYPES = frozenset({"view", "cart", "remove_from_cart", "purchase"})


def unsigned(value: int, name: str, *, positive: bool = False) -> None:
    if type(value) is not int or not (int(positive) <= value <= UINT64_MAX):
        raise ValueError(f"{name}: inteiro entre {int(positive)} e {UINT64_MAX} esperado")


def price_to_cents(value: str) -> int:
    """Não usa float nem arredonda silenciosamente os dados de origem."""
    try:
        amount = Decimal(value)
        cents = amount * 100
        if not cents.is_finite() or cents < 0 or cents > INT64_MAX:
            raise ValueError("preço fora do intervalo permitido")
        if cents != cents.to_integral_value():
            raise ValueError("preço deve ter no máximo duas casas decimais")
        return int(cents)
    except (InvalidOperation, TypeError) as exc:
        raise ValueError(f"preço inválido: {value!r}") from exc


def padded(value: str, width: int, name: str) -> bytes:
    if not isinstance(value, str):
        raise ValueError(f"{name}: texto esperado")
    if any(char in value for char in "\n\r\x00"):
        raise ValueError(f"{name}: quebra de linha ou NUL não permitido")
    encoded = value.encode("utf-8")
    if len(encoded) > width:
        raise ValueError(f"{name}: excede {width} bytes UTF-8")
    return encoded.ljust(width, b" ")


def unpad(value: bytes) -> str:
    return value.rstrip(b" ").decode("utf-8")


def validate_price(value: int) -> None:
    if type(value) is not int or not 0 <= value <= INT64_MAX:
        raise ValueError("price_cents: inteiro não negativo de 64 bits esperado")


@dataclass(frozen=True)
class Product:
    product_id: int
    category_id: int
    price_cents: int
    brand: str
    category_code: str


@dataclass(frozen=True)
class Access:
    access_id: int
    product_id: int
    user_id: int
    price_cents: int
    event_time: str
    event_type: str
    user_session: str


T = TypeVar("T", Product, Access)


class FixedCodec(Generic[T]):
    layout: struct.Struct
    key_field: str

    @property
    def record_size(self) -> int:
        return self.layout.size + 1

    def key(self, record: T) -> int:
        return getattr(record, self.key_field)

    def raw_key(self, raw: bytes) -> int:
        return struct.unpack_from("<Q", raw)[0]

    def unpack_fields(self, raw: bytes) -> tuple:
        if len(raw) != self.record_size or raw[-1:] != b"\n":
            raise ValueError("registro truncado ou terminador inválido")
        return self.layout.unpack(raw[:-1])

    def encode(self, record: T) -> bytes:
        raise NotImplementedError

    def decode(self, raw: bytes) -> T:
        raise NotImplementedError


class ProductCodec(FixedCodec[Product]):
    layout = struct.Struct("<QQq64s128s")
    key_field = "product_id"

    def encode(self, record: Product) -> bytes:
        unsigned(record.product_id, "product_id", positive=True)
        unsigned(record.category_id, "category_id")
        validate_price(record.price_cents)
        return self.layout.pack(
            record.product_id, record.category_id, record.price_cents,
            padded(record.brand, 64, "brand"),
            padded(record.category_code, 128, "category_code"),
        ) + b"\n"

    def decode(self, raw: bytes) -> Product:
        key, category, price, brand, code = self.unpack_fields(raw)
        return Product(key, category, price, unpad(brand), unpad(code))


class AccessCodec(FixedCodec[Access]):
    layout = struct.Struct("<QQQq24s16s36s")
    key_field = "access_id"

    def encode(self, record: Access) -> bytes:
        unsigned(record.access_id, "access_id", positive=True)
        unsigned(record.product_id, "product_id", positive=True)
        unsigned(record.user_id, "user_id")
        validate_price(record.price_cents)
        if record.event_type not in EVENT_TYPES:
            raise ValueError(f"event_type inválido: {record.event_type!r}")
        timestamp = record.event_time
        if len(timestamp) != 23 or not timestamp.endswith(" UTC"):
            raise ValueError("event_time deve ter formato AAAA-MM-DD HH:MM:SS UTC")
        try:
            parsed = datetime.fromisoformat(timestamp[:-4])
        except ValueError as exc:
            raise ValueError("event_time inválido") from exc
        if parsed.strftime("%Y-%m-%d %H:%M:%S UTC") != timestamp:
            raise ValueError("event_time deve ter formato AAAA-MM-DD HH:MM:SS UTC")
        return self.layout.pack(
            record.access_id, record.product_id, record.user_id, record.price_cents,
            padded(timestamp, 24, "event_time"),
            padded(record.event_type, 16, "event_type"),
            padded(record.user_session, 36, "user_session"),
        ) + b"\n"

    def decode(self, raw: bytes) -> Access:
        key, product, user, price, time, event, session = self.unpack_fields(raw)
        return Access(key, product, user, price, unpad(time), unpad(event), unpad(session))
