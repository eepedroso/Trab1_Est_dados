# Relatório técnico e roteiro de apresentação

## Problema

Representar um catálogo de produtos e seus eventos de acesso por organização
sequencial indexada, implementando buscas e atualizações diretamente em memória
secundária. O [dataset indicado no enunciado](https://www.kaggle.com/datasets/mkechinov/ecommerce-behavior-data-from-multi-category-store)
é a fonte da carga; a amostra incluída é explicitamente sintética.

## Modelagem

O catálogo usa `product_id` como chave. Como o mesmo produto aparece em vários
eventos e seus atributos podem variar, a ordenação externa estável conserva a
**primeira observação** encontrada na ordem dos arquivos informados. Isso produz
um registro por produto e uma regra determinística para conflitos. Não se afirma
que esse seja o preço atual do produto. O evento mantém seu próprio preço.

O arquivo de acessos usa `access_id` gerado, pois usuário, sessão, produto e data
podem se repetir. A chave sequencial permite escrever os eventos já ordenados na
carga, sem supor que os CSVs estejam cronologicamente ordenados. A ordem por chave
é a ordem de leitura das fontes. Não se deduplicam eventos: duas linhas iguais
recebem identificadores distintos.

Marcas, categorias, usuários e tipos de evento são campos repetidos. Os dois
arquivos possuem campos únicos e repetidos, além de mais de três colunas cada.
Não há identificadores numéricos convertidos para `float`, preservando categorias
com valores maiores que `2**53`.

## Representação física

Os arquivos não contêm cabeçalho de dados. O registro de posição `i` começa em
`i * record_size`. Produtos têm 217 bytes e acessos têm 109 bytes, incluindo LF.
Os números são binários little-endian; textos são UTF-8 com espaços à direita.
Limites são definidos em bytes, porque caracteres acentuados podem ocupar mais de
um byte. A largura não depende do conteúdo nem do sistema operacional.

O LF atende ao terminador solicitado. Bytes LF internos são possíveis em inteiros
binários, portanto a unidade de leitura é o **registro de tamanho fixo**, não uma
linha de texto. Essa distinção é demonstrada em um teste com a chave 10.

## Carga inicial e memória

1. Ler um evento por vez com `csv.DictReader`, opcionalmente sobre `gzip.open`.
2. Validar campos e escrever o acesso com a próxima chave sequencial.
3. Acumular no máximo `chunk_records` candidatos a produto, ordenar um bloco e
   gravar um run em disco, conservando a primeira observação de cada chave no bloco.
4. Intercalar até `fan_in` runs por rodada com uma heap que mantém um registro
   corrente de cada run. Empates favorecem o run anterior. Eliminar duplicatas.
5. Repetir as rodadas até obter o catálogo ordenado e deduplicado.
6. Construir os dois índices por varredura sequencial e substituir os arquivos.

O padrão de 50 mil registros corresponde a 10,85 MB de conteúdo binário de produtos
por bloco, além do custo dos objetos Python e da ordenação. A memória do bloco é
limitada pela configuração, independentemente do número total de eventos.
O limite de fan-in evita abrir milhares de runs simultaneamente.

As pesquisas não usam esses blocos. Elas mantêm apenas um registro/entrada por
leitura e posições numéricas. Não existe mapa completo de produtos em RAM, nem
`read()` sem tamanho, `readlines()` ou conversão do arquivo de dados em lista.

## Índice parcial

Adotou-se um nível, com intervalo configurável, padrão 256. Um nível é suficiente
porque o próprio índice é pesquisado binariamente no disco; não precisa caber em
RAM. Níveis adicionais não são necessários para atender ao requisito e aumentariam
a complexidade das atualizações.

Para `N` registros e intervalo `B`, há `ceil(N/B)` entradas. Cada entrada tem 17 bytes
e guarda `(primeira_chave_do_bloco, offset_no_arquivo_de_dados)`. O cabeçalho tem
48 bytes e permite reconhecer índices incompatíveis com tamanho, data ou configuração
dos dados. Por exemplo, para um milhão de registros e `B=256`, são 3.907 entradas,
totalizando 66.467 bytes de índice, incluindo cabeçalho.

### Busca direta

Manter limites `[início, fim)` das posições dos registros. A cada passo, calcular
o meio, executar `seek(meio * tamanho)` e ler um único registro. Comparar a chave
e reduzir o intervalo. Ao final, confirmar igualdade; caso contrário, retornar
ausência. A ausência de uma chave não é inferida por seu número de posição.

### Busca indexada

1. Conferir/carregar os metadados do índice persistido.
2. Fazer busca binária nas entradas do arquivo de índice usando
   `seek(48 + posição * 17)` e leitura de 17 bytes.
3. Encontrar a entrada de maior chave que não ultrapasse a chave consultada.
4. Se não houver predecessora, retornar ausência.
5. Converter o offset encontrado na primeira posição do bloco de dados.
6. Fazer busca binária no intervalo do bloco, limitado a `B` registros.

Com `M=ceil(N/B)`, o custo é `O(log M + log B)` acessos a registros, memória `O(1)`.
Esse índice não garante menos leituras que uma busca binária direta em toda situação;
o objetivo é implementar e demonstrar a organização sequencial indexada.

## Atualizações

Inserções e remoções existem para **ambos** os arquivos, embora o enunciado exija
as atualizações em pelo menos um. A localização inicial usa busca binária. A cópia
para o arquivo temporário usa blocos de até 1 MiB, incluindo ou omitindo o registro
na posição correta. Um `os.replace` publica o resultado, e o índice correspondente
é reconstruído imediatamente. O custo é `O(N)` em tempo e espaço temporário no disco.

Optou-se pela remoção física e pela reconstrução a cada operação para manter uma
invariante fácil de verificar: dados ordenados e índice válido após cada sucesso.
Não há área de overflow nem coleta posterior de tombstones. A estratégia é adequada
para um trabalho acadêmico com predominância de consultas; cargas extensas usam o
importador em lote.

Produtos referenciados por acessos não são removidos pela API do repositório.
Inserir acesso exige produto existente. IDs de acessos excluídos não são reutilizados;
a reserva da próxima chave é salva antes da inserção. Uma falha pode deixar lacunas.

## Persistência e integridade

Ao abrir a base, carrega-se a configuração e os cabeçalhos dos dois índices.
Entradas não são materializadas em RAM. Ao fechar, confere-se a persistência dos
índices e dos metadados. A abertura reconstrói índices ausentes ou incompatíveis.
O comando `validar` faz auditoria completa em fluxo de tamanho, layout, ordem,
unicidade e correspondência dos offsets. `--referencias` acrescenta uma busca
indexada de produto por acesso, com custo maior e sem carregar o catálogo.

A trava exclusiva do sistema operacional evita duas instâncias na mesma pasta.
Uso direto da classe de armazenamento fora do repositório pressupõe exclusão
externa. Alterações manuais nos binários não são suportadas. O par de substituições
de uma importação não forma uma transação: uma falha entre arquivos exige reimportar
ou restaurar backup, conforme explicado no README.

## Roteiro de demonstração

1. Importar a amostra em uma pasta nova com `--intervalo 3 --bloco 2 --fan-in 2`.
   Mostrar as contagens: oito produtos, 18 acessos e três/seis entradas de índice.
2. Mostrar todos os produtos: chaves 100, 200, 300, 400, 500, 600, 700, 800.
3. Comparar `buscar produtos 500 --metodo binaria` com `--metodo indice`.
   Consultar também uma chave inexistente e o último acesso.
4. Mostrar as três perguntas com `consultar produto`, `consultar usuario` e
   `consultar resumo`. Para 300, a soma das duas compras é 167.980 centavos.
5. Inserir o produto 250. Mostrar que ele aparece entre 200 e 300 e que é localizado
   pelo índice reconstruído. Tentar inserir 250 novamente: a operação deve falhar.
6. Inserir um acesso ao produto 250, anotar o ID retornado e tentar remover o
   produto: a referência deve bloquear a remoção.
7. Remover o acesso e depois o produto. Executar `validar --referencias`.
8. Encerrar e abrir outro comando `buscar`: os índices persistidos são reutilizados.
9. Executar a suíte e mostrar o teste que impede leituras sem tamanho nas buscas.

## Limites da verificação realizada

A suíte automatizada usa arquivos temporários e dados sintéticos controlados.
Ela valida algoritmos e casos de fronteira, mas não constitui um benchmark sobre
os sete meses reais. Nenhuma análise ou estatística da amostra deve ser atribuída
à população do dataset. A importação completa e a medição do tempo/tamanho do
dataset real ficam reproduzíveis pelos comandos documentados.
