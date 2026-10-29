"""Operações em disco, sem carregar tabelas ou índices inteiros na RAM."""

from collections.abc import Iterable, Iterator
import os
from pathlib import Path
import struct
import tempfile
import time
from typing import BinaryIO, Generic

from .models import FixedCodec, T, unsigned

INDEX_MAGIC = b"ECOMIDX1"
INDEX_VERSION = 1
# magic, versão, intervalo, tamanho do registro, contagem, bytes, mtime_ns.
INDEX_HEADER = struct.Struct("<8sIIQQQQ")
INDEX_ENTRY = struct.Struct("<QQ")
INDEX_ENTRY_SIZE = INDEX_ENTRY.size + 1
COPY_BUFFER_BYTES = 1024 * 1024


def durable_flush(file: BinaryIO) -> None:
    file.flush()
    os.fsync(file.fileno())


def atomic_replace(source: Path, target: Path) -> None:
    """Tolera breves travas de sincronizadores/antivírus no Windows."""
    for attempt in range(6):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if os.name != "nt" or attempt == 5:
                raise
            time.sleep(0.02 * 2**attempt)


def copy_bytes(source: BinaryIO, target: BinaryIO, count: int | None = None) -> None:
    """Copia no máximo 1 MiB por vez, inclusive durante inserções/remoções."""
    while count is None or count > 0:
        size = COPY_BUFFER_BYTES if count is None else min(COPY_BUFFER_BYTES, count)
        block = source.read(size)
        if not block:
            if count:
                raise ValueError("arquivo terminou durante cópia")
            break
        target.write(block)
        if count is not None:
            count -= len(block)


class IndexedSequentialFile(Generic[T]):
    def __init__(self, path: Path, codec: FixedCodec[T], interval: int = 256):
        if type(interval) is not int or not 1 <= interval <= 2**32 - 1:
            raise ValueError("intervalo deve ser inteiro positivo de 32 bits")
        self.path = Path(path)
        self.index_path = self.path.with_suffix(".idx")
        self.codec = codec
        self.interval = interval
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.open("xb").close()
        except FileExistsError:
            pass
        self._fingerprint = None
        self._ensure_index()

    @property
    def record_size(self) -> int:
        return self.codec.record_size

    @property
    def count(self) -> int:
        size = self.path.stat().st_size
        if size % self.record_size:
            raise ValueError(f"{self.path.name}: tamanho não é múltiplo de {self.record_size}")
        return size // self.record_size

    def _signature(self) -> tuple[int, int]:
        stat = self.path.stat()
        return stat.st_size, stat.st_mtime_ns

    def _expected_header(self) -> tuple:
        size, mtime = self._signature()
        return (INDEX_MAGIC, INDEX_VERSION, self.interval, self.record_size,
                self.count, size, mtime)

    def _ensure_index(self) -> None:
        expected = self._expected_header()
        try:
            with self.index_path.open("rb") as index:
                raw = index.read(INDEX_HEADER.size)
            entries = (self.count + self.interval - 1) // self.interval
            valid = (len(raw) == INDEX_HEADER.size
                     and INDEX_HEADER.unpack(raw) == expected
                     and self.index_path.stat().st_size == INDEX_HEADER.size + entries * INDEX_ENTRY_SIZE)
        except OSError:
            valid = False
        if valid:
            # Carrega somente metadados. As entradas continuam no arquivo .idx.
            self._fingerprint = expected
        else:
            self.rebuild_index()

    def rebuild_index(self) -> None:
        """Uma passagem sequencial, mantendo apenas um registro por vez."""
        expected = self._expected_header()
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=self.path.parent,
                                             prefix="index-", delete=False) as index:
                temporary = Path(index.name)
                index.write(INDEX_HEADER.pack(*expected))
                previous = 0
                with self.path.open("rb") as data:
                    for position in range(self.count):
                        raw = self._read_at(data, position)
                        self.codec.unpack_fields(raw)
                        key = self.codec.raw_key(raw)
                        if key <= previous:
                            raise ValueError("chaves devem ser positivas, únicas e crescentes")
                        if position % self.interval == 0:
                            index.write(INDEX_ENTRY.pack(key, position * self.record_size) + b"\n")
                        previous = key
                durable_flush(index)
            atomic_replace(temporary, self.index_path)
            self._fingerprint = expected
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def save_index(self) -> None:
        """Confere/persiste ao encerrar; mutações já persistem imediatamente."""
        self._ensure_index()

    def _read_at(self, file: BinaryIO, position: int) -> bytes:
        file.seek(position * self.record_size)
        raw = file.read(self.record_size)
        if len(raw) != self.record_size or raw[-1:] != b"\n":
            raise ValueError("registro truncado ou terminador inválido")
        return raw

    def _lower_bound(self, file: BinaryIO, key: int, low: int, high: int) -> int:
        while low < high:
            middle = (low + high) // 2
            if self.codec.raw_key(self._read_at(file, middle)) < key:
                low = middle + 1
            else:
                high = middle
        return low

    def binary_search(self, key: int) -> T | None:
        unsigned(key, "chave", positive=True)
        count = self.count
        with self.path.open("rb") as data:
            position = self._lower_bound(data, key, 0, count)
            if position < count:
                raw = self._read_at(data, position)
                if self.codec.raw_key(raw) == key:
                    return self.codec.decode(raw)
        return None

    def indexed_search(self, key: int) -> T | None:
        unsigned(key, "chave", positive=True)
        self._ensure_index()
        count = self.count
        entries = (count + self.interval - 1) // self.interval
        if not entries:
            return None
        # Busca a última entrada com chave <= chave consultada, em disco.
        low, high = 0, entries
        offset = None
        with self.index_path.open("rb") as index:
            while low < high:
                middle = (low + high) // 2
                index.seek(INDEX_HEADER.size + middle * INDEX_ENTRY_SIZE)
                raw = index.read(INDEX_ENTRY_SIZE)
                if len(raw) != INDEX_ENTRY_SIZE or raw[-1:] != b"\n":
                    raise ValueError("entrada de índice inválida")
                current_key, current_offset = INDEX_ENTRY.unpack(raw[:-1])
                if current_key <= key:
                    offset = current_offset
                    low = middle + 1
                else:
                    high = middle
        if offset is None:
            return None
        if offset % self.record_size or offset >= count * self.record_size:
            raise ValueError("offset de índice inválido")
        start = offset // self.record_size
        end = min(start + self.interval, count)
        with self.path.open("rb") as data:
            position = self._lower_bound(data, key, start, end)
            if position < end:
                raw = self._read_at(data, position)
                if self.codec.raw_key(raw) == key:
                    return self.codec.decode(raw)
        return None

    def show(self, limit: int | None = 10, start: int = 0) -> Iterator[T]:
        if start < 0 or (limit is not None and limit < 0):
            raise ValueError("início e limite devem ser não negativos")
        count = self.count
        end = count if limit is None else min(start + limit, count)
        with self.path.open("rb") as data:
            data.seek(start * self.record_size)
            for _ in range(start, end):
                yield self.codec.decode(data.read(self.record_size))

    def insert_initial(self, records: Iterable[T]) -> int:
        """Recebe iterador já ordenado, valida e substitui atomicamente o arquivo."""
        temporary = None
        total = 0
        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=self.path.parent,
                                             prefix="initial-", delete=False) as target:
                temporary = Path(target.name)
                previous = 0
                for record in records:
                    raw = self.codec.encode(record)
                    key = self.codec.key(record)
                    if key <= previous:
                        raise ValueError("carga inicial deve ter chaves únicas em ordem crescente")
                    target.write(raw)
                    previous = key
                    total += 1
                durable_flush(target)
            atomic_replace(temporary, self.path)
            self.rebuild_index()
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return total

    def insert(self, record: T) -> None:
        raw = self.codec.encode(record)
        key = self.codec.key(record)
        count = self.count
        with self.path.open("rb") as data:
            position = self._lower_bound(data, key, 0, count)
            if position < count and self.codec.raw_key(self._read_at(data, position)) == key:
                raise ValueError(f"chave duplicada: {key}")
        self._rewrite(position, raw, remove=False)

    def remove(self, key: int) -> bool:
        unsigned(key, "chave", positive=True)
        count = self.count
        with self.path.open("rb") as data:
            position = self._lower_bound(data, key, 0, count)
            if position == count or self.codec.raw_key(self._read_at(data, position)) != key:
                return False
        self._rewrite(position, b"", remove=True)
        return True

    def _rewrite(self, position: int, raw: bytes, *, remove: bool) -> None:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=self.path.parent,
                                             prefix="rewrite-", delete=False) as target:
                temporary = Path(target.name)
                with self.path.open("rb") as source:
                    copy_bytes(source, target, position * self.record_size)
                    if remove:
                        source.seek(self.record_size, os.SEEK_CUR)
                    else:
                        target.write(raw)
                    copy_bytes(source, target)
                durable_flush(target)
            atomic_replace(temporary, self.path)
            self.rebuild_index()
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def validate(self) -> dict:
        """Auditoria completa, sequencial, com uso constante de memória."""
        self._ensure_index()
        previous = 0
        checked = 0
        with self.path.open("rb") as data, self.index_path.open("rb") as index:
            index.seek(INDEX_HEADER.size)
            for position in range(self.count):
                raw = self._read_at(data, position)
                record = self.codec.decode(raw)
                if self.codec.encode(record) != raw:
                    raise ValueError("registro fora do layout canônico")
                key = self.codec.key(record)
                if key <= previous:
                    raise ValueError("chaves duplicadas ou fora de ordem")
                if position % self.interval == 0:
                    expected = INDEX_ENTRY.pack(key, position * self.record_size) + b"\n"
                    if index.read(INDEX_ENTRY_SIZE) != expected:
                        raise ValueError("entrada de índice não corresponde aos dados")
                previous = key
                checked += 1
        return {"file": self.path.name, "records": checked, "record_size": self.record_size,
                "index_entries": (checked + self.interval - 1) // self.interval,
                "interval": self.interval, "levels": 1}
