"""Importação incremental de CSV/CSV.GZ; produtos ordenados externamente."""

from collections.abc import Callable, Iterable
import csv
import gzip
from pathlib import Path
import tempfile

from .external_sort import sort_products
from .models import Access, AccessCodec, Product, ProductCodec, price_to_cents
from .repository import EcommerceRepository
from .storage import IndexedSequentialFile, atomic_replace, durable_flush

REQUIRED_COLUMNS = {"event_time", "event_type", "product_id", "category_id",
                    "category_code", "brand", "price", "user_id", "user_session"}


def import_csv(repository: EcommerceRepository, paths: Iterable[str | Path], *,
               max_records: int | None = None, chunk_records: int = 50_000,
               fan_in: int = 32, replace: bool = False,
               progress: Callable[[int], None] | None = None) -> dict:
    """Substitui a base; nunca acrescenta silenciosamente os mesmos eventos."""
    sources = [Path(path) for path in paths]
    if not sources:
        raise ValueError("informe pelo menos um CSV")
    if max_records is not None and max_records < 1:
        raise ValueError("max_records deve ser positivo")
    if (repository.products.count or repository.accesses.count) and not replace:
        raise ValueError("base já possui dados; use --substituir para reimportar")
    stats = {"accesses": 0, "products": 0, "first_event": None, "last_event": None,
             "limit": max_records, "sources": []}
    for path in sources:
        stats["sources"].append({"name": path.name, "bytes": path.stat().st_size})
    access_codec = AccessCodec()
    product_codec = ProductCodec()
    with tempfile.TemporaryDirectory(prefix="import-", dir=repository.directory) as temporary:
        workdir = Path(temporary)
        access_path = workdir / "acessos.bin"
        with access_path.open("wb") as access_output:
            def products_from_csv():
                for path in sources:
                    opener = gzip.open if path.suffix.lower() == ".gz" else open
                    with opener(path, "rt", encoding="utf-8-sig", newline="") as source:
                        reader = csv.DictReader(source)
                        if not REQUIRED_COLUMNS.issubset(reader.fieldnames or []):
                            missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
                            raise ValueError(f"{path.name}: colunas ausentes: {sorted(missing)}")
                        for row in reader:
                            if max_records is not None and stats["accesses"] >= max_records:
                                return
                            try:
                                if None in row or any(value is None for value in row.values()):
                                    raise ValueError("quantidade de colunas inválida")
                                cents = price_to_cents(row["price"])
                                product_id = int(row["product_id"])
                                product = Product(product_id, int(row["category_id"] or "0"),
                                                  cents, row["brand"], row["category_code"])
                                access = Access(stats["accesses"] + 1, product_id,
                                                int(row["user_id"]), cents, row["event_time"],
                                                row["event_type"], row["user_session"])
                                product_codec.encode(product)
                                raw = access_codec.encode(access)
                            except (ValueError, TypeError, OverflowError) as exc:
                                raise ValueError(f"{path.name}: linha {reader.line_num}: {exc}") from exc
                            access_output.write(raw)
                            stats["accesses"] += 1
                            timestamp = access.event_time
                            if stats["first_event"] is None or timestamp < stats["first_event"]:
                                stats["first_event"] = timestamp
                            if stats["last_event"] is None or timestamp > stats["last_event"]:
                                stats["last_event"] = timestamp
                            if progress and stats["accesses"] % 100_000 == 0:
                                progress(stats["accesses"])
                            yield product

            sorted_path = sort_products(products_from_csv(), workdir,
                                        chunk_records=chunk_records, fan_in=fan_in)
            durable_flush(access_output)
        if not stats["accesses"]:
            raise ValueError("nenhum registro encontrado nos CSVs")
        product_path = workdir / "produtos.bin"
        atomic_replace(sorted_path, product_path)
        interval = repository.products.interval
        # Valida ordenação e constrói os dois índices antes de tocar na base atual.
        staged_products = IndexedSequentialFile(product_path, product_codec, interval)
        staged_accesses = IndexedSequentialFile(access_path, access_codec, interval)
        stats["products"] = staged_products.count
        # O commit entre arquivos não é uma transação; veja recuperação na documentação.
        for staged, current in ((staged_products, repository.products),
                                (staged_accesses, repository.accesses)):
            atomic_replace(staged.path, current.path)
            atomic_replace(staged.index_path, current.index_path)
        repository.metadata["next_access_id"] = stats["accesses"] + 1
        repository.metadata["import"] = stats
        repository._save_metadata()
    return stats
