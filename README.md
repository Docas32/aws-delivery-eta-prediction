# Análise e previsão do tempo de ciclo de pedidos

Este projeto estima o tempo de ciclo de pedidos com entrega confirmada, usando dados da camada ouro no Amazon S3. O primeiro experimento considera ciclos entre **20 e 120 minutos**, incluindo os limites. O modelo apresentou **MAE de 11,22 minutos**, contra **11,57 minutos** de uma previsão constante pela mediana do treino: ganho de **3,06%**, ainda pequeno para justificar uma promessa precisa de entrega ao cliente.

Os resultados abaixo são da execução verificada em **13/10/2026**, sobre dados históricos de **2021**. Não representam uma avaliação da operação atual.

## Objetivo e alcance

Prever `order_metric_cycle_time`, interpretado em minutos, a partir de informações disponíveis na criação do pedido. A métrica é tratada como **tempo de ciclo**, pois ainda falta confirmar formalmente sua definição no sistema de origem.

A avaliação responde: “Qual o desempenho para pedidos entregues cujo ciclo observado ficou entre 20 e 120 minutos?”. Ela não mede o desempenho para todos os pedidos recebidos, cancelamentos, entregas mais rápidas ou atrasos acima de duas horas. Na criação de um pedido novo, ainda não sabemos se ele pertencerá a esse recorte.

## Fonte e construção da ouro

| Item | Configuração |
| --- | --- |
| Tabela criada no Athena | `delivery_prata_db.ouro_ciclo_entregas` |
| Arquivos Parquet | `s3://delivery-ouro-p/ciclo_entregas/` |
| Região | `sa-east-1` |
| Perfil local | `default` |
| Usuário IAM esperado | `docas32` |
| Alvo | `order_metric_cycle_time` |
| Seleção de entrega | `delivery_status = 'DELIVERED'` |

A transformação da ouro combina `orders`, `stores` e `hubs` com as chaves de `deliveries` que tenham pelo menos um registro `DELIVERED`. As chaves de entregas são consolidadas antes da junção para impedir a multiplicação de pedidos. A leitura local exige `order_id` preenchido e único.

`delivery_status` indica que existe uma entrega concluída associada. `order_status` é mantido para auditoria; o código não exige `FINISHED`. Nenhum dos dois status é usado como atributo preditivo, pois o resultado final não está disponível na criação do pedido.

O script e o notebook leem somente a ouro, diretamente do S3. Não consultam a prata, o Athena ou o catálogo Glue durante a execução e não escrevem nos buckets de origem.

## Por que o alvo foi alterado

A primeira abordagem calculava criação até `order_moment_delivered`. Na prata, esse horário estava ausente em 349.398 de 368.999 pedidos, aproximadamente 94,7%. A coluna já era temporal; `NaT` representava um valor ausente, não um problema que pudesse ser corrigido apenas com conversão de tipo.

Também não substituímos entrega por `order_moment_finished`: nos registros concluídos com ambos os horários, a mediana de finalização menos entrega foi 53,5 minutos. São eventos distintos.

Nos casos `FINISHED` com entrega observada e métrica de ciclo, o ciclo coincidiu com criação até entrega em 96,4% das comparações, com tolerância de 0,02 minuto. Esse resultado apoia a escolha experimental, mas não prova a definição da métrica nos registros sem horário de entrega.

## Qualidade dos dados e recorte de negócio

Na nova ouro, 343.936 linhas correspondem a 343.936 pedidos distintos.

| Faixa do alvo | Pedidos | Percentual da ouro |
| --- | ---: | ---: |
| Ausente | 688 | 0,20% |
| Abaixo de 20 minutos | 8.593 | 2,50% |
| Entre 20 e 120 minutos | 314.214 | 91,36% |
| Acima de 120 minutos | 20.441 | 5,94% |

Não foram encontrados valores não positivos nessa consulta. A ouro completa tinha máximo de 154.277,32 minutos, aproximadamente 107 dias. O grupo acima de 120 minutos tinha mediana de 574,65 minutos, aproximadamente 9,58 horas. Essas observações exigem investigação, não comprovam por si só erro de registro.

**Decisão do experimento:** conservar a ouro completa e filtrar localmente entregas `DELIVERED`, alvo finito entre 20 e 120 e horário de criação válido. Não substituímos alvos ausentes ou extremos pela moda. A faixa é uma hipótese de escopo aprovada para análise, não uma prova de que entregas fora dela sejam impossíveis.

No recorte, a mediana exata calculada pelo pandas foi 41,40 minutos; a média foi 44,61 minutos e o P99 foi 101,95 minutos. Pequenas diferenças em relação às consultas exploratórias são esperadas quando o Athena usa percentis aproximados.

## Método de previsão

Os atributos atuais são `store_id`, latitude e longitude da loja, valor do pedido, hora, dia da semana e mês da criação. Coordenadas representam a loja; não representam a distância até o cliente. Horários posteriores, status finais, alvo e identificadores de pedidos não entram no modelo.

O pipeline combina:

- Imputação de atributos numéricos ausentes pela mediana aprendida no treino.
- One-hot da loja, agrupando categorias raras e limitando a quantidade de categorias.
- `HistGradientBoostingRegressor`, com 150 iterações como configuração inicial e semente 42.

O pré-processamento é ajustado somente no treino. Não foi realizada busca de hiperparâmetros usando o teste. A referência é `DummyRegressor`, que prevê a mediana do alvo do treino para todos os pedidos.

## Separação temporal

Reservamos aproximadamente os 20% mais recentes para teste, mantendo empates de horário do mesmo lado. O corte foi **09/04/2021 às 20:01:16**, segundo a convenção temporal da fonte; não foi confirmada sua correspondência com um fuso específico.

Para aproximar a disponibilidade do alvo, usamos o instante mais tardio entre criação + ciclo e finalização observada, quando presente. O treino exige que esse instante anteceda o corte. Criação + ciclo é uma estimativa de término, não uma data observada de entrega. O instante de ingestão não está disponível para comprovar quando o valor entrou efetivamente no sistema.

| Conjunto | Pedidos |
| --- | ---: |
| Recorte preparado | 314.214 |
| Treino | 251.260 |
| Teste | 62.843 |
| Pedidos anteriores ao corte com alvo ainda indisponível pela aproximação | 111 |

## Resultados observados

As métricas foram conferidas no modelo e no CSV salvos pelo projeto.

| Medida | Resultado |
| --- | ---: |
| MAE do modelo | 11,22 minutos |
| RMSE do modelo | 14,82 minutos |
| MAE da referência pela mediana | 11,57 minutos |
| Redução relativa de MAE | 3,06% |
| Redução absoluta de MAE | 0,35 minuto, aproximadamente 21 segundos |
| Previsões com erro absoluto até 10 minutos | 54,41% |
| Previsões com erro absoluto até 20 minutos | 86,73% |
| Percentil 90 do erro absoluto | 22,45 minutos |
| Previsões abaixo do ciclo observado | 40,49% |
| Viés médio: previsão menos observado | +0,54 minuto |

MAE é o erro absoluto médio, não uma garantia de erro máximo. RMSE dá mais peso a erros grandes. O viés próximo de zero não significa que erros individuais sejam pequenos: subestimações e superestimações podem se compensar.

O ganho é preliminar. Uma única divisão temporal não estabelece estabilidade em outros períodos nem significância estatística da diferença para a referência.

## Como os resultados impactam o negócio

| Uso potencial | Implicação do resultado | Limite atual |
| --- | --- | --- |
| Estimativa exibida ao cliente | Pode oferecer uma estimativa adaptada à loja e ao horário, em vez de uma duração única. | Cerca de 45,59% dos pedidos de teste erraram por mais de 10 minutos. Não há intervalo de previsão calibrado nem evidência suficiente para um prazo garantido. |
| Gestão de expectativa | Subestimar o tempo pode gerar percepção de atraso, contatos e insatisfação. | 40,49% das previsões ficaram abaixo do ciclo observado. Isso não equivale à taxa real de reclamação ou de descumprimento de uma promessa. |
| Planejamento operacional | Estimativas podem auxiliar a leitura de padrões por loja e período. | O ganho sobre a mediana é de apenas 21 segundos de MAE, em média. Não foi medido ganho de capacidade, escala ou produtividade. |
| Tratamento de atrasos severos | A ouro completa permite investigar a cauda e diferenças de operação. | O treino exclui ciclos acima de 120 minutos; o modelo não foi validado para detectar ou estimar esses atrasos. |
| Cancelamentos e retenção | Uma previsão melhor pode motivar um experimento de comunicação. | A amostra contém entregas confirmadas. Não foi treinado um modelo de cancelamento nem demonstrada redução de cancelamentos ou aumento de retenção. |
| Retorno financeiro | O resultado permite comparar o benefício preditivo com o custo de operar o modelo. | Não há dados de custo, receita ou experimento causal para quantificar economia ou retorno financeiro. |

**Interpretação de negócio:** a implementação demonstra viabilidade técnica e boa cobertura dentro da faixa escolhida, mas o ganho atual é modesto. Não há evidência de que implantar esse modelo gere, por si só, economia, redução de cancelamentos ou melhoria de satisfação. A referência simples continua sendo uma comparação importante antes de assumir custo operacional adicional.

O filtro usa a duração real, conhecida somente depois do pedido. Na operação, não é possível selecionar antecipadamente os pedidos que ficarão entre 20 e 120 minutos. Por isso, antes de usar a previsão com clientes, é necessário avaliar todos os pedidos elegíveis na criação e definir como tratar casos fora do recorte.

## Como executar

No PowerShell, a partir da pasta do projeto:

```powershell
.\venv\Scripts\Activate.ps1
python main.py
```

Configure o perfil `default` com as credenciais autorizadas de `docas32` e confirme localmente a identidade:

```powershell
aws sts get-caller-identity --profile default
```

Não coloque chaves no código, no notebook ou no repositório. São necessários acesso STS para confirmar identidade, `s3:ListBucket` para o prefixo da ouro e `s3:GetObject` para os arquivos. Se houver criptografia com chave KMS gerenciada pelo cliente, confirme a permissão de descriptografia.

As bibliotecas usadas incluem `awswrangler`, `boto3`, `pandas`, `numpy`, `scikit-learn` e `joblib`. O notebook também usa IPython/Jupyter. O projeto foi executado com o ambiente `venv` existente; este README não estabelece versões fixadas ou uma instalação reproduzível a partir do zero.

Para análise interativa, abra `analise.ipynb`, selecione o kernel do `venv` e execute as células em ordem. Ele contém diagnóstico da base completa, aplicação do filtro, análise exploratória, treino, comparação e exemplo de previsão.

## Arquivos gerados

| Arquivo | Conteúdo |
| --- | --- |
| `main.py` | Fluxo executável de leitura, preparação, treinamento e avaliação. |
| `analise.ipynb` | Etapas comentadas, decisões e inspeção interativa. |
| `resultados/previsoes_ciclo_teste.csv` | ID, criação, alvo observado, previsão e erro absoluto de cada pedido do teste. |
| `resultados/modelo_tempo_ciclo.joblib` | Pipeline, atributos, alvo, fonte, faixa, status, corte e métricas. |

O modelo salvo é o avaliado no corte temporal, sem retreinamento com o teste. Cada execução substitui os arquivos de ciclo na pasta de resultados. Artefatos antigos com `tempo_total` ou `previsoes_teste` no nome pertencem à abordagem anterior e não devem ser confundidos com esta avaliação.

## Próximos passos para uma decisão de implantação

1. Confirmar a definição e a unidade de `order_metric_cycle_time` na origem e o fuso dos horários.
2. Investigar ausências e ciclos extremos, preservando a rastreabilidade dos registros.
3. Validar em múltiplos períodos e com dados atuais, pois esta avaliação usa pedidos de 2021.
4. Medir erros por loja, horário e volume, comparando também referências simples por grupo aprendidas apenas no treino.
5. Avaliar cobertura sobre todos os pedidos e definir tratamento de atrasos e cancelamentos fora desta amostra.
6. Investigar atributos disponíveis na criação, como destino, distância estimada e carga operacional, caso existam e possam ser usados.
7. Definir a tolerância de erro do negócio e avaliar intervalos de previsão antes de exibir prazos ao cliente.
8. Medir resultados em um piloto controlado: cumprimento da promessa, contatos ao suporte, satisfação e cancelamentos, além do erro preditivo. Só então estimar retorno financeiro.
