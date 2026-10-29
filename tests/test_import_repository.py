import csv
import gzip
import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from ecommerce.cli import main
from ecommerce.external_sort import read_products, sort_products
from ecommerce.importer import import_csv
from ecommerce.models import Product, price_to_cents
from ecommerce.repository import EcommerceRepository

SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "amostra_sintetica.csv"


class ImportRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.data = self.directory / "data"

    def tearDown(self):
        self.temp.cleanup()

    def load(self, repository, **kwargs):
        return import_csv(repository, [SAMPLE], chunk_records=2, fan_in=2, **kwargs)

    def test_demo_full_import_sort_dedup_queries_and_reopen(self):
        with EcommerceRepository(self.data, 3) as repository:
            stats = self.load(repository)
            self.assertEqual((stats["products"], stats["accesses"]), (8, 18))
            self.assertEqual([p.product_id for p in repository.products.show(None)], list(range(100, 801, 100)))
            self.assertEqual(repository.products.indexed_search(300).price_cents, 89990)
            self.assertEqual(repository.accesses.indexed_search(3).price_cents, 87990)
            self.assertEqual(repository.product_summary(300), {
                "product_id": 300, "events": {"view": 1, "cart": 1, "remove_from_cart": 0, "purchase": 2},
                "purchase_value_cents": 167980})
            self.assertEqual(len(list(repository.user_accesses(1001))), 7)
            self.assertEqual(len(list(repository.user_accesses(1001, "00000000-0000-0000-0000-000000000003"))), 3)
            self.assertEqual(repository.product_details(400)["brand"], "")
            self.assertEqual(repository.validate(references=True)["references"], "ok")
        with EcommerceRepository(self.data) as repository:
            self.assertEqual(repository.products.interval, 3)
            self.assertEqual(repository.accesses.indexed_search(18).product_id, 300)

    def test_external_sort_multiple_passes_stable_first_observation(self):
        records = [Product(key, 0, number, "", "") for number, key in enumerate([9, 2, 9, 1, 2, 5, 1, 8, 4, 9])]
        result = sort_products(iter(records), self.directory, chunk_records=1, fan_in=2)
        actual = list(read_products(result))
        self.assertEqual([(p.product_id, p.price_cents) for p in actual],
                         [(1, 3), (2, 1), (4, 8), (5, 5), (8, 7), (9, 0)])

    def test_gzip_multiple_sources_and_global_limit(self):
        compressed = self.directory / "sample.csv.gz"
        with gzip.open(compressed, "wt", encoding="utf-8", newline="") as target:
            target.write(SAMPLE.read_text(encoding="utf-8"))
        with EcommerceRepository(self.data) as repository:
            stats = import_csv(repository, [compressed, SAMPLE], max_records=20, chunk_records=3, fan_in=2)
            self.assertEqual(stats["accesses"], 20)
            self.assertEqual(stats["products"], 8)
            self.assertEqual(repository.accesses.indexed_search(19).product_id, 300)

    def test_import_failure_preserves_existing_files(self):
        invalid = self.directory / "invalid.csv"
        invalid.write_text(SAMPLE.read_text(encoding="utf-8").replace("879.90", "879.901"), encoding="utf-8")
        with EcommerceRepository(self.data) as repository:
            self.load(repository)
            before = (repository.products.path.read_bytes(), repository.accesses.path.read_bytes())
            with self.assertRaisesRegex(ValueError, "linha 4"):
                import_csv(repository, [invalid], replace=True, chunk_records=1, fan_in=2)
            self.assertEqual(before, (repository.products.path.read_bytes(), repository.accesses.path.read_bytes()))
            self.assertEqual(repository.validate(references=True)["references"], "ok")

    def test_reimport_requires_explicit_replace(self):
        with EcommerceRepository(self.data) as repository:
            self.load(repository)
            with self.assertRaisesRegex(ValueError, "substituir"):
                self.load(repository)
            self.assertEqual(self.load(repository, replace=True, max_records=2)["accesses"], 2)

    def test_empty_and_missing_columns_rejected(self):
        for text in ("wrong,columns\n1,2\n", SAMPLE.read_text(encoding="utf-8").splitlines()[0] + "\n"):
            source = self.directory / "invalid.csv"
            source.write_text(text, encoding="utf-8")
            with EcommerceRepository(self.data) as repository, self.assertRaises(ValueError):
                import_csv(repository, [source])

    def test_mutations_reference_guard_and_access_id_not_reused(self):
        with EcommerceRepository(self.data) as repository:
            self.load(repository)
            with self.assertRaisesRegex(ValueError, "possui acessos"):
                repository.remove_product(300)
            repository.insert_product(Product(250, 0, 500, "nova", ""))
            self.assertTrue(repository.remove_product(250))
            self.assertTrue(repository.remove_access(18))
        with EcommerceRepository(self.data) as repository:
            record = repository.insert_access(product_id=300, user_id=1001, price_cents=9900,
                                               event_time="2020-04-08 10:00:00 UTC", event_type="view")
            self.assertEqual(record.access_id, 19)
            with self.assertRaisesRegex(ValueError, "produto inexistente"):
                repository.insert_access(product_id=999, user_id=1001, price_cents=9900,
                                         event_time="2020-04-08 10:00:00 UTC", event_type="view")
            self.assertEqual(repository.metadata["next_access_id"], 20)

    def test_lock_prevents_concurrent_use_and_releases(self):
        with EcommerceRepository(self.data):
            with self.assertRaises(RuntimeError):
                EcommerceRepository(self.data)
        with EcommerceRepository(self.data) as repository:
            self.assertEqual(repository.products.count, 0)

    def test_prices_validation(self):
        self.assertEqual(price_to_cents("10.00"), 1000)
        self.assertEqual(price_to_cents("0.01"), 1)
        for bad in ("NaN", "Infinity", "-1.00", "1.001", "invalido", str(2**63)):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                price_to_cents(bad)

    def test_cli_import_search_query_and_error_exit_codes(self):
        prefix = ["--dados", str(self.data)]

        def call(*arguments):
            output, error = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(error):
                code = main(prefix + list(arguments))
            return code, output.getvalue(), error.getvalue()

        self.assertEqual(call("importar", str(SAMPLE), "--bloco", "2", "--fan-in", "2")[0], 0)
        for method in ("indice", "binaria"):
            code, output, _ = call("buscar", "produtos", "300", "--metodo", method)
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output)["product_id"], 300)
        self.assertEqual(call("buscar", "produtos", "999")[0], 1)
        self.assertEqual(call("remover", "produtos", "300")[0], 2)
        self.assertEqual(call("validar", "--referencias")[0], 0)
        self.assertEqual(call("inserir", "produto", "--id", "250", "--categoria-id", "0", "--preco", "5.50")[0], 0)
        self.assertEqual(call("remover", "produtos", "250")[0], 0)


if __name__ == "__main__":
    unittest.main()
