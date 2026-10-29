# E-commerce: organização sequencial indexada

Projeto em **Python 3.10+**, sem dependências de execução externas, para um trabalho
de estrutura de dados e organização de arquivos. Implementa dois arquivos binários
com registros de tamanho fixo, buscas diretamente em disco e um índice parcial
persistente para cada arquivo.

## Dataset e escopo

Fonte: [eCommerce behavior data from multi category store — Michael Kechinov / Kaggle](https://www.kaggle.com/datasets/mkechinov/ecommerce-behavior-data-from-multi-category-store),
com atribuição à [REES46 Marketing Platform](https://rees46.com/).
A descrição cobre outubro/2019 a abril/2020. A página do Kaggle disponibiliza
outubro e novembro e indica arquivos adicionais para os demais meses.

**Os dados reais não estão incluídos neste repositório.** Baixe os arquivos pela
página original. O importador aceita um ou vários `.csv` / `.csv.gz`, sem precisar
descompactar antecipadamente. Para cobrir os sete meses, forneça os sete arquivos.
Uma execução com um mês ou com `--max-registros` representa somente esse recorte.

Há uma [amostra sintética](examples/README.md) com 18 eventos e oito produtos para
executar e verificar o programa sem baixar os arquivos grandes. Ela não foi extraída
do Kaggle. Os direitos e condições dos dados de origem pertencem aos respectivos
autores; consulte a fonte antes de redistribuí-los.

## Executar em poucos passos

Na pasta do projeto, usando Python 3.10 ou superior:

```powershell
python -m ecommerce --dados data --intervalo 3 importar examples/amostra_sintetica.csv --bloco 2 --fan-in 2
python -m ecommerce --dados data mostrar produtos --todos
python -m ecommerce --dados data buscar produtos 300 --metodo binaria
python -m ecommerce --dados data buscar produtos 300 --metodo indice
python -m ecommerce --dados data buscar acessos 3 --metodo indice
python -m ecommerce --dados data consultar resumo 300
python -m ecommerce --dados data validar --referencias
python -m unittest discover -s tests -v
```

No Windows, se seu Python estiver disponível pelo launcher, substitua `python` por
`py -3`. Não é necessário usar `pip install` para executar da pasta do projeto.
Opcionalmente, `python -m pip install -e .` instala o comando `ecommerce`.

Os exemplos usam intervalo 3 para tornar o índice visível em uma base pequena.
O padrão para uma nova base é **256**. A configuração é salva: quando `--intervalo`
é omitido nas próximas execuções, o programa reutiliza o intervalo da base.
As opções globais `--dados` e `--intervalo` vêm **antes** do subcomando.

## Carga do dataset real

Coloque os CSVs baixados em `raw/`, pasta ignorada pelo Git. Exemplo com um mês:

```powershell
python -m ecommerce --dados data-real importar raw/2019-Oct.csv.gz
```

Para os sete meses, informe os arquivos explicitamente, na ordem desejada:

```powershell
python -m ecommerce --dados data-real importar raw/2019-Oct.csv.gz raw/2019-Nov.csv.gz raw/2019-Dec.csv.gz raw/2020-Jan.csv.gz raw/2020-Feb.csv.gz raw/2020-Mar.csv.gz raw/2020-Apr.csv.gz
```

Para experimentar um recorte de 100 mil eventos:

```powershell
python -m ecommerce --dados data-recorte importar raw/2019-Oct.csv.gz --max-registros 100000 --bloco 10000
```

`--max-registros` é um limite global, somando todos os arquivos informados.
`--bloco` limita a quantidade de registros na ordenação inicial dos produtos
(padrão 50 mil); `--fan-in` limita os runs abertos por rodada (padrão 32).
O programa lê cada CSV sequencialmente, escreve acessos já ordenados pela chave
gerada e ordena/deduplica os produtos por **intercalação externa**. Não usa pandas,
SQLite ou um dicionário com todos os produtos. A ordenação é estável: o catálogo
mantém a primeira observação de cada produto na ordem dos arquivos informados.
O preço de cada evento é preservado separadamente no arquivo de acessos.

Uma base com dados só pode ser reimportada com `--substituir`. Isso substitui
produtos e acessos, reinicia as chaves de acesso e descarta as alterações manuais
da base anterior. Faça backup ou use outra pasta `--dados` para preservar uma base.
Linhas inválidas interrompem a preparação sem substituir os dados existentes.
Arquivos grandes exigem tempo e espaço em disco para dados, runs e cópias temporárias.

## Arquivos e chaves

| Arquivo | Conteúdo | Chave única | Campos que podem se repetir |
| --- | --- | --- | --- |
| `produtos.bin` | catálogo deduplicado, ordenado por produto | `product_id` original | categoria, marca, preço |
| `acessos.bin` | eventos, ordenados por acesso | `access_id` sequencial, a partir de 1 | produto, usuário, sessão, tipo, preço, data |
| `produtos.idx` | índice parcial de produtos | primeira chave de cada bloco | offsets dos blocos |
| `acessos.idx` | índice parcial de acessos | primeira chave de cada bloco | offsets dos blocos |
| `metadata.json` | configuração, próxima chave, proveniência e resumo da carga | — | metadados pequenos |
| `.repository.lock` | trava de exclusão entre processos | — | liberada pelo sistema operacional |

Os dois arquivos de dados têm mais de três campos. A unicidade e a ordem das
chaves são verificadas na carga, nas mutações e na auditoria. As chaves dos acessos
não são recalculadas após remoções; a próxima chave é persistida para evitar reuso.

### Layout binário

Os registros são codificados com `struct`, ordem **little-endian**, sem alinhamento
implícito. Inteiros não são gravados como dígitos textuais. Preços são inteiros em
centavos, evitando erro de arredondamento com `float`. Campos textuais usam UTF-8
e espaços (`0x20`) à direita até completar a largura em **bytes**. Valores maiores
que o campo são rejeitados, sem truncamento silencioso. Campos opcionais de texto
vazios são preenchidos com espaços; categoria numérica ausente recebe zero.

| Produto (`<QQq64s128s` + LF) | Offset | Bytes |
| --- | ---: | ---: |
| `product_id` (uint64) | 0 | 8 |
| `category_id` (uint64) | 8 | 8 |
| `price_cents` (int64 não negativo) | 16 | 8 |
| `brand` | 24 | 64 |
| `category_code` | 88 | 128 |
| terminador `\n` | 216 | 1 |
| **Total** | | **217** |

| Acesso (`<QQQq24s16s36s` + LF) | Offset | Bytes |
| --- | ---: | ---: |
| `access_id` (uint64) | 0 | 8 |
| `product_id` (uint64) | 8 | 8 |
| `user_id` (uint64) | 16 | 8 |
| `price_cents` (int64 não negativo) | 24 | 8 |
| `event_time` (`AAAA-MM-DD HH:MM:SS UTC`) | 32 | 24 |
| `event_type` | 56 | 16 |
| `user_session` | 72 | 36 |
| terminador `\n` | 108 | 1 |
| **Total** | | **109** |

Cada registro termina com o byte `b'\n'`, gravado em modo `wb`. Como campos numéricos
binários podem conter o mesmo byte internamente, **não se usa `readline()` nem
`splitlines()`**. A leitura sempre respeita o tamanho fixo e o deslocamento é
`posição * tamanho_do_registro`. Assim, o Windows não converte LF para CRLF.

## Índices parciais persistentes

Foi escolhido **um nível**, com uma entrada para cada **256 registros** por padrão.
Cada entrada contém a primeira chave do bloco e seu offset em bytes no arquivo de
dados: `<QQ` + LF, total de **17 bytes**. Um cabeçalho de 48 bytes (`<8sIIQQQQ`)
contém assinatura, versão, intervalo, tamanho do registro, contagem de registros,
tamanho do arquivo de dados e `mtime_ns`.

Exemplo com intervalo 3 para produtos com chaves 100 a 800:

```text
produtos.idx                 produtos.bin (217 bytes por registro)
chave 100 -> offset    0 --> [100] [200] [300]
chave 400 -> offset  651 --> [400] [500] [600]
chave 700 -> offset 1302 --> [700] [800]
```

Uma consulta por 500 faz busca binária **no arquivo `.idx`**, encontra a última
chave menor ou igual a 500 (400), usa `seek(651)` para delimitar o bloco e faz
busca binária apenas nos seus até três registros. O último bloco pode ser menor.
Chaves ausentes, arquivos vazios e chaves fora do intervalo são tratados.

O índice é construído e persistido na carga e após cada inserção/remoção. Ao fechar,
`save_index()` verifica que está persistido. Ao abrir, o cabeçalho é carregado e
conferido; **as entradas do índice continuam no disco**, inclusive durante a busca.
Um índice ausente, truncado ou com metadados desatualizados é reconstruído por uma
passagem sequencial. `validar` audita todas as entradas contra os dados; o cabeçalho
não substitui uma verificação criptográfica de corrupção arbitrária.

## Três perguntas implementadas

1. **Quais são os dados de um produto pelo seu identificador?**
   `consultar produto 300` usa o índice parcial de produtos.
2. **Quais acessos um usuário realizou, opcionalmente em uma sessão?**
   `consultar usuario 1001` ou `consultar usuario 1001 --sessao UUID` percorre acessos
   sequencialmente, emitindo um resultado por vez, pois o índice é por `access_id`.
3. **Quantas visualizações, adições/remoções de carrinho e compras ocorreram para
   um produto e qual a soma dos valores dos seus eventos de compra?**
   `consultar resumo 300` percorre acessos, mantendo só quatro contadores e a soma.

O resumo usa o preço de cada evento; o preço do catálogo é apenas a primeira
observação. Cada linha `purchase` contribui com seu preço uma vez. Não há campo de
quantidade, frete, desconto total do pedido ou devolução: a soma representa os
valores observados nos eventos de compra, não uma apuração contábil de receita.

## Inserir e remover

```powershell
python -m ecommerce inserir produto --id 250 --categoria-id 10 --preco 99.90 --marca exemplo --categoria electronics.audio
python -m ecommerce inserir acesso --produto 250 --usuario 1001 --preco 99.90 --data "2020-04-08 10:00:00 UTC" --tipo view --sessao "00000000-0000-0000-0000-000000000009"
python -m ecommerce remover acessos 19
python -m ecommerce remover produtos 250
```

O ID 19 corresponde à inserção após a amostra original de 18 acessos, sem outras
inserções prévias. A saída da inserção informa o ID realmente atribuído.

**Estratégia:** localizar a posição com busca binária, copiar sequencialmente para
um arquivo temporário inserindo ou omitindo o registro, substituir o arquivo com
`os.replace()` e reconstruir imediatamente o índice afetado. A cópia usa blocos de
até 1 MiB; não carrega a tabela. A remoção é física, sem tombstones. A inserção de
chave duplicada é rejeitada; remover uma chave ausente não modifica os arquivos.

As duas tabelas oferecem as seis operações exigidas. Na camada de repositório,
inserir acesso exige produto existente e remover produto com acessos é bloqueado.
Remova primeiro os acessos relacionados se quiser excluir o produto.

## Correspondência com o enunciado

| Exigência | Implementação |
| --- | --- |
| Carga inicial em cada arquivo | `IndexedSequentialFile.insert_initial()`; carga conjunta com `import_csv()` |
| Mostrar dados parcial/total | `show(limit, start)`; comando `mostrar` |
| Busca binária direta no arquivo | `binary_search()`; `buscar --metodo binaria` |
| Consulta por índice parcial | `indexed_search()`; `buscar --metodo indice` |
| Inserir registros | `insert()`; repositório `insert_product()` / `insert_access()` |
| Remover registros | `remove()`; repositório `remove_product()` / `remove_access()` |
| Dois índices persistentes | `produtos.idx` e `acessos.idx`, conferidos na abertura e no fechamento |
| Ordenação pela chave | produtos: ordenação externa; acessos: sequência gerada |
| Registros binários fixos e LF | codecs de 217 / 109 bytes em `models.py` |
| Pesquisas sem carregar arquivos na RAM | `seek()` e `read(tamanho_fixo)` nos dados e nos índices |

## Verificação e estrutura

```powershell
python -m ecommerce info
python -m ecommerce validar
python -m ecommerce validar --referencias
python -m unittest discover -s tests -v
```

Os testes cobrem limites dos blocos, gaps nas chaves, arquivos vazios, leitura
limitada durante buscas, persistência, corrupção detectável, ordenação externa
com várias rodadas, CSV/GZIP, falha de importação, mutações e as consultas. O
GitHub Actions está configurado para executar a suíte em Linux/Windows com
Python 3.10/3.12; a execução remota depende do GitHub Actions estar habilitado.

Saídas são JSON; listagens são JSON por registro, emitido incrementalmente.
Preços aparecem em centavos. Códigos de saída: `0` sucesso, `1` chave não encontrada,
`2` erro de uso/dados/operação e `130` interrupção pelo teclado.

```text
ecommerce/
  models.py         layouts, validação e codificação binária
  storage.py        arquivos sequenciais e índices parciais em disco
  external_sort.py  runs limitados e intercalação externa
  importer.py       carga de CSV/CSV.GZ em fluxo
  repository.py     integração, consultas e próxima chave
  locking.py        exclusão entre processos
  cli.py            comandos em português
examples/           amostra sintética e resultado esperado
tests/              testes automatizados com unittest
docs/RELATORIO.md   decisões, custos e roteiro de apresentação
.github/workflows/  execução automatizada de testes
```

## Custos e limites práticos

Para `N` registros, intervalo `B` e `M = ceil(N/B)` entradas, busca direta faz
`O(log N)` leituras; busca indexada faz `O(log M + log B)` leituras, com memória
`O(1)`. Consultas por usuário/produto nos acessos são `O(N)`, com memória constante.
Reconstrução do índice e mutações são `O(N)`; o arquivo temporário custa espaço
adicional proporcional ao arquivo alterado. Essa estratégia favorece consultas e
uma demonstração simples de atualizações; inserir um evento por vez numa base de
centenas de milhões de linhas é caro. Faça a carga inicial em lote.

A ordenação externa usa até `--bloco` registros e até `--fan-in` registros correntes
na intercalação, mais buffers de E/S e caminhos dos runs. Não carrega a base toda.
O programa aplica uma trava exclusiva por pasta de dados. O arquivo da trava
permanece no disco, e o sistema libera a trava quando o processo termina.

Cada substituição de um arquivo de dados é atômica; dados e índice são substituídos
separadamente. Se uma interrupção deixar o índice antigo, a próxima abertura detecta
a diferença de tamanho/data e o refaz. A importação prepara ambos os arquivos antes
da substituição, mas **o conjunto dos dois arquivos não é uma transação**. Uma falha
do sistema exatamente entre substituições pode deixar a importação incompleta:
nesse caso, execute novamente a mesma carga com `--substituir`, ou restaure o backup.
Não há garantia de recuperação transacional contra perda de energia.

## GitHub

Os arquivos grandes, dados gerados e credenciais estão no `.gitignore`.
O repositório deve conter código, documentação, testes e a amostra sintética.
Uma vez criado o repositório privado e configurado seu remoto, use o fluxo usual
de `git add`, `git commit` e `git push`. Nunca adicione `kaggle.json` ao controle de versão.
