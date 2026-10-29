A amostra `amostra_sintetica.csv` foi criada apenas para demonstração e testes.
Seus 18 eventos, oito produtos, usuários, sessões e preços são fictícios.
Ela segue as nove colunas do dataset indicado, inclui os quatro tipos de evento,
campos opcionais vazios, preços variáveis e eventos de outubro/2019 a abril/2020.
Ela **não é um recorte extraído do Kaggle** e não permite conclusões sobre a loja real.

Importação que força vários runs para demonstrar a ordenação externa:

```powershell
python -m ecommerce --intervalo 3 importar examples/amostra_sintetica.csv --bloco 2 --fan-in 2
python -m ecommerce mostrar produtos --todos
python -m ecommerce consultar resumo 300
```

Para o produto 300, a resposta esperada é uma visualização, uma adição ao carrinho,
duas compras e `purchase_value_cents: 167980` (soma de 879,90 e 799,90).
