"""Previsao do tempo de ciclo de pedidos entregues entre 20 e 120 minutos."""

from pathlib import Path

import awswrangler as wr
import boto3
import joblib
import numpy as np
import pandas as pd
from botocore.exceptions import ClientError
from sklearn.compose import ColumnTransformer
from sklearn.dummy import DummyRegressor
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.metrics import mean_absolute_error, mean_squared_error
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder

REGIAO = "sa-east-1"
PERFIL_AWS = "default"
USUARIO_AWS = "docas32"
CAMADA_OURO = "s3://delivery-ouro-p/ciclo_entregas/"
LIMITE_MINUTOS_MIN = 20
LIMITE_MINUTOS_MAX = 120
RESULTADOS = Path(__file__).resolve().parent / "resultados"
ALVO = "order_metric_cycle_time"
NUMERICAS = ["store_latitude", "store_longitude", "order_amount", "hora", "dia_semana", "mes"]
ATRIBUTOS = ["store_id", *NUMERICAS]


def carregar_dados(sessao):
    # A nova ouro ja consolida pedidos, lojas e a existencia de entrega DELIVERED.
    # Lemos somente esta fonte; nao precisamos de acesso a prata ou ao Glue.
    # Parquet e lido pelo conteudo, inclusive arquivos CTAS sem extensao .parquet.
    print("Lendo a camada ouro...", flush=True)
    dados = wr.s3.read_parquet(
        path=CAMADA_OURO, boto3_session=sessao,
        columns=["order_id", "delivery_order_id", "store_id", "store_latitude",
                 "store_longitude", "order_amount", "order_status", "delivery_status",
                 "order_moment_created", "order_moment_finished", ALVO],
    )
    if dados["order_id"].isna().any() or dados["order_id"].duplicated().any():
        raise ValueError("A ouro deve ter order_id preenchido e uma unica linha por pedido.")
    print(f"Pedidos na ouro: {len(dados):,}")
    return dados


def preparar_dados(dados):
    dados = dados.copy()
    for coluna in ["order_moment_created", "order_moment_finished"]:
        dados[coluna] = pd.to_datetime(dados[coluna], errors="coerce")
    # Alvo vem pronto da ouro: nao depende de order_moment_delivered, muito ausente.
    dados[ALVO] = pd.to_numeric(dados[ALVO], errors="coerce")
    entregues = dados["delivery_status"].eq("DELIVERED").fillna(False)
    finitos = dados[ALVO].notna() & np.isfinite(dados[ALVO])
    faixa = dados[ALVO].between(LIMITE_MINUTOS_MIN, LIMITE_MINUTOS_MAX)
    # Recorte aprovado do experimento, com limites inclusivos. Nao imputamos
    # duracoes pela moda nem alteramos os arquivos completos da ouro.
    print(f"Alvos ausentes ou nao finitos: {(~finitos).sum():,}")
    print(f"Abaixo de 20 minutos: {(finitos & dados[ALVO].lt(LIMITE_MINUTOS_MIN)).sum():,}")
    print(f"Acima de 120 minutos: {(finitos & dados[ALVO].gt(LIMITE_MINUTOS_MAX)).sum():,}")
    print(f"Status diferente de DELIVERED: {(~entregues).sum():,}")
    print(f"Instante de criacao ausente: {dados['order_moment_created'].isna().sum():,}")
    validos = entregues & finitos & faixa & dados["order_moment_created"].notna()
    print(f"Pedidos para o experimento: {validos.sum():,} de {len(dados):,}")
    dados = dados.loc[validos].sort_values("order_moment_created").copy()
    # Criacao + ciclo e uma estimativa de fim do ciclo, nao uma data observada.
    # Para o corte temporal usamos o mais tardio entre essa estimativa e a
    # finalizacao observada, quando existe, como aproximacao conservadora da
    # disponibilidade do alvo. O instante real de ingestao nao esta na ouro.
    fim_estimado = dados["order_moment_created"] + pd.to_timedelta(dados[ALVO], unit="m")
    dados["disponibilidade_alvo"] = pd.concat(
        [fim_estimado, dados["order_moment_finished"]], axis=1
    ).max(axis=1)
    dados["store_id"] = dados["store_id"].fillna("desconhecida").astype(str)
    dados["hora"] = dados["order_moment_created"].dt.hour
    dados["dia_semana"] = dados["order_moment_created"].dt.dayofweek
    dados["mes"] = dados["order_moment_created"].dt.month
    for coluna in NUMERICAS:
        dados[coluna] = pd.to_numeric(dados[coluna], errors="coerce").replace([np.inf, -np.inf], np.nan)
    return dados


def treinar(dados):
    if len(dados) < 50:
        raise ValueError("Sao necessarios pelo menos 50 pedidos validos para esta avaliacao.")
    # Os 20% mais recentes ficam para teste, simulando previsoes futuras.
    corte = dados.iloc[int(len(dados) * 0.8)]["order_moment_created"]
    # Evita usar alvos ainda indisponiveis no corte segundo a aproximacao acima.
    treino = dados.loc[(dados["order_moment_created"] < corte) & (dados["disponibilidade_alvo"] < corte)]
    teste = dados.loc[dados["order_moment_created"] >= corte]
    if len(treino) < 30 or len(teste) < 10:
        raise ValueError("O corte temporal nao produziu treino e teste suficientes.")

    # Apenas atributos disponiveis na criacao entram no modelo.
    # Tempo de cozinha e horarios posteriores causariam vazamento de informacao.
    tratamento = ColumnTransformer([
        ("numeros", SimpleImputer(strategy="median", keep_empty_features=True), NUMERICAS),
        ("loja", OneHotEncoder(handle_unknown="infrequent_if_exist", min_frequency=20,
                               max_categories=100, sparse_output=False), ["store_id"]),
    ])
    modelo = Pipeline([
        ("tratamento", tratamento),
        ("regressor", HistGradientBoostingRegressor(max_iter=150, random_state=42)),
    ])
    modelo.fit(treino[ATRIBUTOS], treino[ALVO])
    previsoes = np.maximum(modelo.predict(teste[ATRIBUTOS]), 0)
    # A mediana e uma referencia simples que o modelo deve superar.
    baseline = DummyRegressor(strategy="median").fit(treino[ATRIBUTOS], treino[ALVO])
    mae_base = mean_absolute_error(teste[ALVO], baseline.predict(teste[ATRIBUTOS]))
    mae = mean_absolute_error(teste[ALVO], previsoes)
    rmse = np.sqrt(mean_squared_error(teste[ALVO], previsoes))
    print(f"Treino: {len(treino):,}; teste: {len(teste):,}; corte: {corte}")
    print(f"MAE: {mae:.2f} min | RMSE: {rmse:.2f} min | MAE da mediana: {mae_base:.2f} min")
    if mae >= mae_base:
        print("O modelo ainda nao supera a mediana; revise atributos e qualidade dos dados.")

    RESULTADOS.mkdir(parents=True, exist_ok=True)
    saida = teste[["order_id", "order_moment_created", ALVO]].copy()
    saida["previsao_minutos"] = previsoes
    saida["erro_absoluto_minutos"] = abs(saida[ALVO] - previsoes)
    saida.to_csv(RESULTADOS / "previsoes_ciclo_teste.csv", index=False)
    # Salva o modelo avaliado, sem retreinar com os dados reservados para teste.
    joblib.dump({"modelo": modelo, "atributos": ATRIBUTOS, "alvo": ALVO,
                 "corte_temporal": corte, "mae_minutos": mae,
                 "rmse_minutos": rmse, "mae_baseline_minutos": mae_base,
                 "fonte": CAMADA_OURO, "status": "DELIVERED",
                 "faixa_minutos": [LIMITE_MINUTOS_MIN, LIMITE_MINUTOS_MAX]},
                RESULTADOS / "modelo_tempo_ciclo.joblib")
    print(f"Modelo e previsoes salvos em: {RESULTADOS}")
    return modelo, saida


def main():
    # Seleciona explicitamente o perfil padrao do arquivo de credenciais AWS.
    # O nome do perfil (default) e diferente do nome do usuario IAM (docas32).
    sessao = boto3.Session(profile_name=PERFIL_AWS, region_name=REGIAO)
    # STS confirma a identidade real; mudar somente o nome do usuario no codigo
    # nao troca as credenciais. Interrompemos antes de ler S3 se o usuario divergir.
    identidade = sessao.client("sts").get_caller_identity()["Arn"]
    if not identidade.endswith(f":user/{USUARIO_AWS}") and not (
        ":user/" in identidade and identidade.rsplit("/", 1)[-1] == USUARIO_AWS
    ):
        raise SystemExit(
            f"O perfil {PERFIL_AWS!r} autentica como {identidade}, mas o usuario "
            f"esperado e {USUARIO_AWS}. Configure localmente as credenciais de "
            f"{USUARIO_AWS} no perfil default (aws configure --profile default) "
            "e execute novamente. Nao coloque chaves neste codigo ou no chat."
        )
    print(f"Usuario AWS confirmado: {USUARIO_AWS}", flush=True)
    dados = preparar_dados(carregar_dados(sessao))
    print("\nDistribuicao do ciclo no recorte de 20 a 120 minutos:")
    print(dados[ALVO].describe(percentiles=[0.5, 0.9, 0.95, 0.99]).to_string())
    treinar(dados)


if __name__ == "__main__":
    try:
        main()
    except ClientError as erro:
        codigo = erro.response.get("Error", {}).get("Code", "desconhecido")
        if codigo in {"AccessDenied", "AccessDeniedException", "403"}:
            raise SystemExit(
                "Acesso AWS negado. Libere s3:ListBucket no bucket delivery-ouro-p "
                "e s3:GetObject em ciclo_entregas/*. "
                "Se houver criptografia KMS, confira kms:Decrypt.\n"
                f"Detalhe AWS: {erro}"
            ) from erro
        raise
