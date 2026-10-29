from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

from ecommerce.models import Access, AccessCodec, Product, ProductCodec
from ecommerce.storage import INDEX_ENTRY_SIZE, INDEX_HEADER, IndexedSequentialFile, atomic_replace


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def make_table(self, codec=None, interval=3):
        codec = codec or ProductCodec()
        return IndexedSequentialFile(self.directory / f"{codec.key_field}.bin", codec, interval)

    def product(self, key):
        return Product(key, 2053013552326770688, 1099, "marca", "categoria")

    def test_sizes_padding_newline_and_large_integer_round_trip(self):
        products = ProductCodec()
        accesses = AccessCodec()
        product = Product(10, 2053013552326770688, 1234, "ação", "")
        raw = products.encode(product)
        self.assertEqual(products.record_size, 217)
        self.assertEqual(accesses.record_size, 109)
        self.assertEqual(len(raw), 217)
        self.assertEqual(raw[-1:], b"\n")
        self.assertEqual(raw[24:88], "ação".encode().ljust(64, b" "))
        self.assertEqual(products.decode(raw), product)
        # O inteiro 10 contém 0x0A: readline/splitlines não pode ler esses arquivos.
        self.assertGreater(raw.count(b"\n"), 1)

    def test_text_limits_are_in_bytes_and_never_truncate(self):
        codec = ProductCodec()
        codec.encode(Product(1, 0, 0, "é" * 32, ""))
        for brand in ("é" * 33, "a\nb", "a\rb", "a\0b"):
            with self.subTest(brand=brand), self.assertRaises(ValueError):
                codec.encode(Product(1, 0, 0, brand, ""))

    def test_empty_singleton_insert_remove_both_files(self):
        for codec, record in (
            (ProductCodec(), self.product(9)),
            (AccessCodec(), Access(9, 2, 3, 100, "2019-10-01 00:00:00 UTC", "view", "")),
        ):
            with self.subTest(codec=type(codec).__name__):
                table = self.make_table(codec)
                self.assertIsNone(table.binary_search(9))
                self.assertIsNone(table.indexed_search(9))
                self.assertFalse(table.remove(9))
                table.insert_initial(iter([record]))
                self.assertEqual(table.binary_search(9), record)
                self.assertEqual(table.indexed_search(9), record)
                self.assertTrue(table.remove(9))
                table.insert(record)
                self.assertEqual(list(table.show(None)), [record])
                table.remove(9)
                self.assertEqual(table.validate()["records"], 0)

    def test_search_every_key_gap_and_partial_last_block(self):
        for interval in (1, 2, 3, 16, 256):
            table = self.make_table(interval=interval)
            records = [self.product(key) for key in range(5, 102, 4)]
            table.insert_initial(iter(records))
            expected = {record.product_id: record for record in records}
            for key in range(1, 108):
                with self.subTest(interval=interval, key=key):
                    self.assertEqual(table.binary_search(key), expected.get(key))
                    self.assertEqual(table.indexed_search(key), expected.get(key))

    def test_index_contains_exact_keys_and_byte_offsets(self):
        table = self.make_table(interval=3)
        table.insert_initial(self.product(key) for key in range(1, 9))
        raw = table.index_path.read_bytes()
        self.assertEqual(len(raw), INDEX_HEADER.size + 3 * INDEX_ENTRY_SIZE)
        import struct
        entries = [struct.unpack("<QQ", raw[start:start + 16])
                   for start in range(INDEX_HEADER.size, len(raw), 17)]
        self.assertEqual(entries, [(1, 0), (4, 3 * 217), (7, 6 * 217)])

    def test_search_reads_only_fixed_sized_pieces(self):
        table = self.make_table(interval=16)
        table.insert_initial(self.product(key * 2) for key in range(1, 2049))
        original_open = Path.open
        reads = []

        class GuardedFile:
            def __init__(self, file):
                self.file = file

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.file.close()

            def __getattr__(self, name):
                return getattr(self.file, name)

            def read(self, size=-1):
                if not 0 <= size <= 217:
                    raise AssertionError(f"leitura não limitada: {size}")
                reads.append(size)
                return self.file.read(size)

        def guarded_open(path, *args, **kwargs):
            file = original_open(path, *args, **kwargs)
            return GuardedFile(file) if args and args[0] == "rb" else file

        with patch.object(Path, "open", guarded_open):
            self.assertEqual(table.indexed_search(4000).product_id, 4000)
            self.assertLess(len(reads), 20)
            reads.clear()
            self.assertEqual(table.binary_search(4000).product_id, 4000)
            self.assertLess(len(reads), 15)

    def test_initial_rejects_duplicates_and_unsorted_without_changing_data(self):
        table = self.make_table()
        table.insert_initial([self.product(9)])
        for keys in ((2, 1), (1, 1), (0,)):
            with self.assertRaises(ValueError):
                table.insert_initial(self.product(key) for key in keys)
            self.assertEqual(list(table.show(None)), [self.product(9)])

    def test_ordered_mutations_at_beginning_middle_end(self):
        table = self.make_table()
        keys = [10, 30, 50, 70, 90]
        random.Random(42).shuffle(keys)
        for key in keys:
            table.insert(self.product(key))
        self.assertEqual([p.product_id for p in table.show(None)], sorted(keys))
        for key in (10, 50, 90, 30, 70):
            self.assertTrue(table.remove(key))
            self.assertIsNone(table.indexed_search(key))
            table.validate()
        self.assertEqual(table.count, 0)

    def test_duplicate_insertion_preserves_data_and_index(self):
        table = self.make_table()
        table.insert(self.product(7))
        before = (table.path.read_bytes(), table.index_path.read_bytes())
        with self.assertRaises(ValueError):
            table.insert(self.product(7))
        self.assertEqual(before, (table.path.read_bytes(), table.index_path.read_bytes()))

    def test_reopen_loads_saved_index_without_rebuilding(self):
        table = self.make_table()
        table.insert_initial(self.product(key) for key in range(1, 10))
        table.save_index()
        with patch.object(IndexedSequentialFile, "rebuild_index", side_effect=AssertionError("rebuild inesperado")):
            reopened = self.make_table()
            self.assertEqual(reopened.indexed_search(5).product_id, 5)

    def test_missing_or_truncated_index_is_rebuilt(self):
        table = self.make_table()
        table.insert(self.product(7))
        table.index_path.unlink()
        self.assertEqual(table.indexed_search(7).product_id, 7)
        table.index_path.write_bytes(b"invalido")
        self.assertEqual(self.make_table().indexed_search(7).product_id, 7)

    def test_changed_data_invalidates_index(self):
        table = self.make_table()
        table.insert(self.product(7))
        with table.path.open("ab") as data:
            data.write(table.codec.encode(self.product(9)))
        self.assertEqual(table.indexed_search(9).product_id, 9)
        self.assertEqual(table.validate()["records"], 2)

    def test_audit_detects_corrupt_index_entry(self):
        table = self.make_table()
        table.insert(self.product(7))
        with table.index_path.open("r+b") as index:
            index.seek(INDEX_HEADER.size)
            index.write(b"\x08")
        with self.assertRaises(ValueError):
            table.validate()

    def test_truncated_data_and_bad_intervals_rejected(self):
        for interval in (0, -1, True, 2**32):
            with self.assertRaises(ValueError):
                self.make_table(interval=interval)
        table = self.make_table()
        table.path.write_bytes(b"incompleto")
        with self.assertRaises(ValueError):
            table.binary_search(1)

    def test_show_is_partial_or_total(self):
        table = self.make_table()
        table.insert_initial(self.product(key) for key in range(1, 11))
        self.assertEqual([p.product_id for p in table.show(3, 4)], [5, 6, 7])
        self.assertEqual(list(table.show(3, 100)), [])
        self.assertEqual(len(list(table.show(None))), 10)
        with self.assertRaises(ValueError):
            list(table.show(-1))

    def test_windows_atomic_replace_retries_transient_lock(self):
        with patch("ecommerce.storage.os.name", "nt"), \
             patch("ecommerce.storage.os.replace", side_effect=[PermissionError("ocupado"), None]) as replace, \
             patch("ecommerce.storage.time.sleep") as sleep:
            atomic_replace("origem", "destino")
            self.assertEqual(replace.call_count, 2)
            sleep.assert_called_once_with(0.02)


if __name__ == "__main__":
    unittest.main()
