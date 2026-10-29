"""Interface de linha de comando, com saída JSON e leitura incremental."""

import argparse
import csv
from dataclasses import asdict
import json
import sys

from .importer import import_csv
from .models import Product, price_to_cents
from .repository import EcommerceRepository


def emit(value) -> None:
    print(json.dumps(value, ensure_ascii=False))


def positive(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("inteiro esperado") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("inteiro positivo esperado")
    return number


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="E-commerce: arquivos sequenciais indexados em disco")
    parser.add_argument("--dados", default="data", help="pasta dos arquivos binários (padrão: data)")
    parser.add_argument("--intervalo", type=positive, help="intervalo do índice; salvo para as próximas execuções")
    sub = parser.add_subparsers(dest="command", required=True)

    load = sub.add_parser("importar", help="carga inicial de CSVs ou CSV.GZ do Kaggle")
    load.add_argument("arquivos", nargs="+")
    load.add_argument("--max-registros", type=positive, help="limite global para um recorte explícito")
    load.add_argument("--bloco", type=positive, default=50_000, help="registros por run de ordenação externa")
    load.add_argument("--fan-in", type=positive, default=32, help="máximo de runs intercalados por rodada")
    load.add_argument("--substituir", action="store_true", help="autoriza substituir uma base existente")

    show = sub.add_parser("mostrar", help="exibe registros sem carregar o arquivo inteiro")
    show.add_argument("arquivo", choices=("produtos", "acessos"))
    show.add_argument("--inicio", type=int, default=0, help="posição inicial, base zero")
    show.add_argument("--limite", type=positive, default=10)
    show.add_argument("--todos", action="store_true")

    search = sub.add_parser("buscar", help="busca binária direta ou por índice parcial")
    search.add_argument("arquivo", choices=("produtos", "acessos"))
    search.add_argument("chave", type=positive)
    search.add_argument("--metodo", choices=("binaria", "indice"), default="indice")

    query = sub.add_parser("consultar", help="as três perguntas definidas no trabalho")
    queries = query.add_subparsers(dest="query", required=True)
    product = queries.add_parser("produto", help="quais os dados do produto?")
    product.add_argument("product_id", type=positive)
    user = queries.add_parser("usuario", help="quais os acessos de um usuário/sessão?")
    user.add_argument("user_id", type=positive)
    user.add_argument("--sessao")
    summary = queries.add_parser("resumo", help="quantos eventos e qual o valor de compras do produto?")
    summary.add_argument("product_id", type=positive)

    insert = sub.add_parser("inserir", help="insere e reconstrói o índice do arquivo afetado")
    inserts = insert.add_subparsers(dest="kind", required=True)
    product = inserts.add_parser("produto")
    product.add_argument("--id", type=positive, required=True)
    product.add_argument("--categoria-id", type=int, required=True)
    product.add_argument("--preco", required=True, help="valor decimal, ex.: 99.90")
    product.add_argument("--marca", default="")
    product.add_argument("--categoria", default="")
    access = inserts.add_parser("acesso")
    access.add_argument("--produto", type=positive, required=True)
    access.add_argument("--usuario", type=positive, required=True)
    access.add_argument("--preco", required=True)
    access.add_argument("--data", required=True, help='"2019-10-01 00:00:00 UTC"')
    access.add_argument("--tipo", choices=("view", "cart", "remove_from_cart", "purchase"), required=True)
    access.add_argument("--sessao", default="")

    remove = sub.add_parser("remover", help="remoção física com reconstrução imediata do índice")
    remove.add_argument("arquivo", choices=("produtos", "acessos"))
    remove.add_argument("chave", type=positive)
    validate = sub.add_parser("validar", help="verifica layouts, ordem, chaves e índices em disco")
    validate.add_argument("--referencias", action="store_true", help="confere cada produto dos acessos (mais lento)")
    sub.add_parser("info", help="contagens, configurações e proveniência da carga")
    return parser


def run(args, repository: EcommerceRepository) -> int:
    if args.command == "importar":
        emit(import_csv(repository, args.arquivos, max_records=args.max_registros,
                        chunk_records=args.bloco, fan_in=args.fan_in, replace=args.substituir,
                        progress=lambda count: print(f"Importados {count:,} acessos...", file=sys.stderr)))
    elif args.command == "mostrar":
        table = repository.products if args.arquivo == "produtos" else repository.accesses
        for record in table.show(None if args.todos else args.limite, args.inicio):
            emit(asdict(record))
    elif args.command == "buscar":
        table = repository.products if args.arquivo == "produtos" else repository.accesses
        method = table.binary_search if args.metodo == "binaria" else table.indexed_search
        record = method(args.chave)
        emit(asdict(record) if record else None)
        return 0 if record else 1
    elif args.command == "consultar":
        if args.query == "produto":
            product = repository.product_details(args.product_id)
            emit(product)
            return 0 if product else 1
        if args.query == "usuario":
            for record in repository.user_accesses(args.user_id, args.sessao):
                emit(asdict(record))
        else:
            emit(repository.product_summary(args.product_id))
    elif args.command == "inserir":
        if args.kind == "produto":
            product = Product(args.id, args.categoria_id, price_to_cents(args.preco), args.marca, args.categoria)
            repository.insert_product(product)
            emit(asdict(product))
        else:
            emit(asdict(repository.insert_access(
                product_id=args.produto, user_id=args.usuario, price_cents=price_to_cents(args.preco),
                event_time=args.data, event_type=args.tipo, user_session=args.sessao)))
    elif args.command == "remover":
        method = repository.remove_product if args.arquivo == "produtos" else repository.remove_access
        removed = method(args.chave)
        emit({"removed": removed, "key": args.chave})
        return 0 if removed else 1
    elif args.command == "validar":
        emit(repository.validate(references=args.referencias))
    elif args.command == "info":
        emit({"products": repository.products.count, "accesses": repository.accesses.count,
              "product_record_bytes": repository.products.record_size,
              "access_record_bytes": repository.accesses.record_size,
              "metadata": repository.metadata})
    return 0


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    try:
        with EcommerceRepository(args.dados, args.intervalo) as repository:
            return run(args, repository)
    except (ValueError, OSError, RuntimeError, csv.Error) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Operação interrompida.", file=sys.stderr)
        return 130
