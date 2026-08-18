from datetime import datetime, timedelta
from statistics import fmean

from app.repositories.store import DemoStore
from app.services.forecast import DIAS_MINIMOS, FUSO_LOJA, ForecastService

PESOS = {0: 0.9, 1: 0.6, 2: 0.95, 3: 1.0, 4: 1.25, 5: 1.8, 6: 1.1}


def _loja_com_historico(tmp_path, dias: int, crescimento_semanal: float = 0.0) -> ForecastService:
    """Cria um historico com padrao conhecido para conferir o que o modelo recupera."""

    store = DemoStore(tmp_path / "store.json")
    hoje = datetime.now(FUSO_LOJA).date()
    for atras in range(dias, 0, -1):
        dia = hoje - timedelta(days=atras)
        semanas = (dias - atras) / 7
        receita = 400.0 * PESOS[dia.weekday()] * (1 + crescimento_semanal * semanas)
        momento = datetime.combine(dia, datetime.min.time(), tzinfo=FUSO_LOJA).replace(hour=15)
        store.put("orders", f"h-{atras}", {
            "id": f"h-{atras}", "number": 1000 + atras, "created_at": momento.isoformat(),
            "channel": "Balcao", "payment_method": "Pix", "customer_name": None, "discount": 0,
            "total": round(receita, 2), "cost": round(receita / 2, 2), "status": "Concluido",
            "items": [{"product_id": "acai-500", "name": "Açaí 500 ml", "quantity": 2,
                       "unit_price": 22.0, "unit_cost": 11.0, "line_total": 44.0}],
        })
    return ForecastService(store)


def test_forecast_refuses_to_guess_without_enough_history(tmp_path):
    """Poucos dias nao permitem separar dia da semana de acaso.

    Mostrar previsao nesse ponto seria dar cara de ciencia a um chute, que e o
    oposto do que o gestor pediu.
    """

    resultado = _loja_com_historico(tmp_path, dias=5).forecast(7)

    assert resultado["ready"] is False
    assert resultado["required_days"] == DIAS_MINIMOS
    assert not resultado["daily_forecast"]


def test_forecast_recovers_the_weekday_pattern(tmp_path):
    resultado = _loja_com_historico(tmp_path, dias=56).forecast(7)

    assert resultado["ready"] is True
    normalizador = fmean(PESOS.values())
    for peso in resultado["weekday_effect"]:
        esperado = PESOS[peso["weekday"]] / normalizador
        assert abs(peso["factor"] - esperado) < 0.05, f"{peso['name']} saiu em {peso['factor']}, esperado {esperado:.2f}"
    sabado = next(p for p in resultado["weekday_effect"] if p["weekday"] == 5)
    assert "acima" in sabado["reading"]


def test_forecast_reports_growth_and_its_own_error(tmp_path):
    resultado = _loja_com_historico(tmp_path, dias=56, crescimento_semanal=0.05).forecast(7)

    # O historico cresce 5% do valor inicial por semana, o que equivale a ~4,2%
    # da media do periodo, que e a unidade em que o modelo reporta.
    assert 3.5 <= resultado["trend"]["percent_per_week"] <= 5.5

    # Mesmo sem ruido sobra ~5% de erro: e o residuo de ajustar uma reta a uma
    # sazonalidade multiplicativa. Numa loja real a variacao diaria e de 20% a
    # 40%, entao refinar abaixo disso seria otimizar sob o ruido do mundo.
    assert resultado["accuracy"]["percent"] < 8
    assert resultado["accuracy"]["mean_absolute_error"] >= 0


def test_forecast_projects_from_the_end_not_the_average(tmp_path):
    """Com crescimento, prever a partir da media do periodo subestima o futuro.

    Foi o erro encontrado ao validar o modelo: ancorado na media, o proximo
    sabado saia menor que o sabado anterior, mesmo com a loja crescendo.
    """

    servico = _loja_com_historico(tmp_path, dias=56, crescimento_semanal=0.05)
    resultado = servico.forecast(7)

    proximo_sabado = next(d for d in resultado["daily_forecast"] if d["weekday"] == "sábado")
    assert proximo_sabado["value"] > resultado["baseline"], "sábado precisa superar a média diária"
    sabado_esperado = 400.0 * PESOS[5] * (1 + 0.05 * 8)
    assert abs(proximo_sabado["value"] - sabado_esperado) / sabado_esperado < 0.15
