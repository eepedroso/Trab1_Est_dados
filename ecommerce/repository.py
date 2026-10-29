"""Integra produtos, acessos, integridade referencial e metadados pequenos."""

from dataclasses import asdict
import json
from pathlib import Path
import tempfile

from .locking import RepositoryLock
from .models import Access, AccessCodec, Product, ProductCodec, UINT64_MAX
from .storage import IndexedSequentialFile, atomic_replace, durable_flush


class EcommerceRepository:
    """Use com ``with`` para salvar índices e liberar a trava ao encerrar."""

    def __init__(self, directory: str | Path = "data", interval: int | None = None):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = RepositoryLock(self.directory / ".repository.lock")
        try:
            self.metadata_path = self.directory / "metadata.json"
            if self.metadata_path.exists():
                self.metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
                if not isinstance(self.metadata, dict) or self.metadata.get("version") != 1:
                    raise ValueError("versão de metadados não suportada")
            else:
                self.metadata = {"version": 1, "next_access_id": 1, "interval": 256}
            chosen_interval = self.metadata["interval"] if interval is None else interval
            self.products = IndexedSequentialFile(self.directory / "produtos.bin", ProductCodec(), chosen_interval)
            self.accesses = IndexedSequentialFile(self.directory / "acessos.bin", AccessCodec(), chosen_interval)
            self.metadata["interval"] = chosen_interval
            last_key = 0
            if self.accesses.count:
                last = next(self.accesses.show(1, self.accesses.count - 1))
                last_key = last.access_id
            next_id = self.metadata.get("next_access_id")
            if type(next_id) is not int or not 1 <= next_id <= UINT64_MAX + 1:
                raise ValueError("next_access_id inválido nos metadados")
            self.metadata["next_access_id"] = max(next_id, last_key + 1)
            self._save_metadata()
        except BaseException:
            self._lock.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def close(self) -> None:
        if self._lock.file.closed:
            return
        try:
            self.products.save_index()
            self.accesses.save_index()
            self._save_metadata()
        finally:
            self._lock.close()

    def _save_metadata(self) -> None:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=self.directory,
                                             prefix="meta-", delete=False) as target:
                temporary = Path(target.name)
                target.write(json.dumps(self.metadata, ensure_ascii=False, indent=2).encode("utf-8"))
                durable_flush(target)
            atomic_replace(temporary, self.metadata_path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def insert_product(self, product: Product) -> None:
        self.products.insert(product)

    def insert_access(self, *, product_id: int, user_id: int, price_cents: int,
                      event_time: str, event_type: str, user_session: str = "") -> Access:
        if self.products.indexed_search(product_id) is None:
            raise ValueError(f"produto inexistente: {product_id}")
        record = Access(self.metadata["next_access_id"], product_id, user_id,
                        price_cents, event_time, event_type, user_session)
        self.accesses.codec.encode(record)
        # Reserva antes da escrita: falhas podem deixar lacunas, nunca reutilizar IDs.
        self.metadata["next_access_id"] += 1
        self._save_metadata()
        self.accesses.insert(record)
        return record

    def remove_product(self, key: int) -> bool:
        if self.products.indexed_search(key) is None:
            return False
        for access in self.accesses.show(limit=None):
            if access.product_id == key:
                raise ValueError("produto possui acessos; remova os acessos relacionados primeiro")
        return self.products.remove(key)

    def remove_access(self, key: int) -> bool:
        return self.accesses.remove(key)

    def product_details(self, key: int) -> dict | None:
        product = self.products.indexed_search(key)
        return asdict(product) if product else None

    def user_accesses(self, user_id: int, session: str | None = None):
        # Não há índice secundário por usuário: varredura sequencial com gerador.
        for access in self.accesses.show(limit=None):
            if access.user_id == user_id and (session is None or access.user_session == session):
                yield access

    def product_summary(self, product_id: int) -> dict:
        if self.products.indexed_search(product_id) is None:
            raise ValueError(f"produto inexistente: {product_id}")
        counts = {"view": 0, "cart": 0, "remove_from_cart": 0, "purchase": 0}
        revenue = 0
        for access in self.accesses.show(limit=None):
            if access.product_id == product_id:
                counts[access.event_type] += 1
                if access.event_type == "purchase":
                    revenue += access.price_cents
        return {"product_id": product_id, "events": counts, "purchase_value_cents": revenue}

    def validate(self, *, references: bool = False) -> dict:
        result = {"products": self.products.validate(), "accesses": self.accesses.validate()}
        if references:
            for access in self.accesses.show(limit=None):
                if self.products.indexed_search(access.product_id) is None:
                    raise ValueError(f"acesso {access.access_id} referencia produto inexistente")
            result["references"] = "ok"
        return result
