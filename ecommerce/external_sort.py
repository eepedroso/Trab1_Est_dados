"""Ordenação externa por intercalação, com limite de registros e de arquivos."""

from collections.abc import Iterable, Iterator
from contextlib import ExitStack
import heapq
from pathlib import Path

from .models import Product, ProductCodec


def _read(file, size: int) -> bytes:
    raw = file.read(size)
    if raw and len(raw) != size:
        raise ValueError("run de ordenação truncado")
    return raw


def _merge(paths: list[Path], output: Path, codec: ProductCodec) -> None:
    """Empates favorecem o run anterior: preserva a primeira observação."""
    with ExitStack() as stack:
        inputs = [stack.enter_context(path.open("rb")) for path in paths]
        target = stack.enter_context(output.open("wb"))
        heap = []
        for number, file in enumerate(inputs):
            raw = _read(file, codec.record_size)
            if raw:
                heapq.heappush(heap, (codec.raw_key(raw), number, raw))
        previous = None
        while heap:
            key, number, raw = heapq.heappop(heap)
            if key != previous:
                target.write(raw)
                previous = key
            following = _read(inputs[number], codec.record_size)
            if following:
                heapq.heappush(heap, (codec.raw_key(following), number, following))


def sort_products(records: Iterable[Product], workdir: Path, *,
                  chunk_records: int = 50_000, fan_in: int = 32) -> Path:
    """Apenas um bloco limitado fica em RAM; os runs intermediários ficam em disco."""
    if chunk_records < 1:
        raise ValueError("chunk_records deve ser positivo")
    if fan_in < 2:
        raise ValueError("fan_in deve ser pelo menos 2")
    codec = ProductCodec()
    iterator = iter(records)
    runs = []
    exhausted = False
    while not exhausted:
        chunk = []
        for _ in range(chunk_records):
            try:
                chunk.append(codec.encode(next(iterator)))
            except StopIteration:
                exhausted = True
                break
        if not chunk:
            break
        # sort é estável; duplicatas preservam a primeira linha na entrada.
        chunk.sort(key=codec.raw_key)
        path = workdir / f"run-0-{len(runs):08d}.bin"
        with path.open("wb") as output:
            previous = None
            for raw in chunk:
                key = codec.raw_key(raw)
                if key != previous:
                    output.write(raw)
                    previous = key
        runs.append(path)
        del chunk
    if not runs:
        empty = workdir / "products-empty.bin"
        empty.touch()
        return empty
    level = 1
    while len(runs) > 1:
        next_runs = []
        for start in range(0, len(runs), fan_in):
            group = runs[start:start + fan_in]
            output = workdir / f"run-{level}-{len(next_runs):08d}.bin"
            _merge(group, output, codec)
            next_runs.append(output)
            for path in group:
                path.unlink()
        runs = next_runs
        level += 1
    return runs[0]


def read_products(path: Path) -> Iterator[Product]:
    codec = ProductCodec()
    with path.open("rb") as file:
        while raw := _read(file, codec.record_size):
            yield codec.decode(raw)
