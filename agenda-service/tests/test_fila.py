"""RF-14 — fila de espera.

A regra que estes testes protegem é a mais contraintuitiva do requisito:
**não há reserva**. Durante a oferta o horário continua livre na grade, e
quem confirmar primeiro leva. Um teste que "arrumasse" isso estaria
testando outro produto.
"""

from datetime import timedelta

from .conftest import integracao

pytestmark = integracao

QUARTA = "2027-04-07"
JANELA = {
    "janela_inicio": f"{QUARTA}T09:00:00-03:00",
    "janela_fim": f"{QUARTA}T18:00:00-03:00",
}
SLOT = f"{QUARTA}T10:00:00-03:00"


def _entrar(client, catalogo, nome="Segunda Pessoa", telefone="+5511922223333", **extra):
    corpo = {
        "service_id": catalogo["servico"]["id"],
        "cliente_nome": nome,
        "cliente_telefone": telefone,
        **JANELA,
        **extra,
    }
    return client.post("/waitlist", json=corpo)


def _agendar(client, catalogo, inicio=SLOT, telefone="+5511911112222"):
    return client.post(
        "/appointments",
        json={
            "service_id": catalogo["servico"]["id"],
            "resource_id": catalogo["recurso"]["id"],
            "inicio": inicio,
            "cliente_nome": "Primeira Pessoa",
            "cliente_telefone": telefone,
        },
    )


def _ofertar(org_id, catalogo, inicio=SLOT):
    """Roda o que o background task roda depois de um cancelamento."""
    from datetime import datetime

    from app import ofertas

    ini = datetime.fromisoformat(inicio)
    return ofertas.ofertar_slot_liberado(
        org_id,
        catalogo["servico"]["id"],
        catalogo["recurso"]["id"],
        ini,
        ini + timedelta(minutes=60),
    )


def test_entrar_na_fila_devolve_posicao_e_janela_falada(client, catalogo):
    resposta = _entrar(client, catalogo)
    assert resposta.status_code == 201, resposta.text
    corpo = resposta.json()
    assert corpo["status"] == "aguardando"
    assert corpo["posicao"] == 1
    assert corpo["janela_humana"].startswith("quarta")
    assert "às" in corpo["janela_humana"]


def test_a_fila_e_fila_a_posicao_segue_a_chegada(client, catalogo):
    primeira = _entrar(client, catalogo, nome="Ana", telefone="+5511900000001").json()
    segunda = _entrar(client, catalogo, nome="Bia", telefone="+5511900000002").json()
    assert (primeira["posicao"], segunda["posicao"]) == (1, 2)

    lista = client.get("/waitlist").json()
    assert [e["cliente_nome"] for e in lista] == ["Ana", "Bia"]


def test_sair_da_fila_e_idempotente(client, catalogo):
    entrada = _entrar(client, catalogo).json()
    assert client.delete(f"/waitlist/{entrada['id']}").json()["status"] == "cancelado"
    assert client.delete(f"/waitlist/{entrada['id']}").json()["status"] == "cancelado"
    assert client.get("/waitlist").json() == []


def test_cancelamento_oferta_ao_primeiro_da_fila(client, catalogo, org_id, canal_fake):
    ap = _agendar(client, catalogo).json()
    _entrar(client, catalogo, nome="Ana", telefone="+5511900000001")
    _entrar(client, catalogo, nome="Bia", telefone="+5511900000002")

    assert client.post(f"/appointments/{ap['id']}/cancel", json={"motivo": "imprevisto"}).status_code == 200

    fila = {e["cliente_nome"]: e for e in client.get("/waitlist").json()}
    assert fila["Ana"]["status"] == "ofertado"
    assert fila["Ana"]["expira_em"] is not None
    assert fila["Bia"]["status"] == "aguardando"  # a vez é de um por vez

    # a mensagem foi por TEMPLATE (nunca texto ad-hoc) e diz o prazo
    (envio,) = canal_fake.enviados
    assert envio["template_nome"] == "fila_oferta"
    assert envio["destinatario"] == "+5511900000001"
    assert envio["variaveis"]["minutos"] == "30"


def test_durante_a_oferta_o_horario_continua_livre_para_qualquer_um(
    client, catalogo, org_id, canal_fake
):
    """Sem hold: é a regra do RF-14. Quem confirmar primeiro leva."""
    ap = _agendar(client, catalogo).json()
    _entrar(client, catalogo, nome="Ana", telefone="+5511900000001")
    client.post(f"/appointments/{ap['id']}/cancel", json={"motivo": "imprevisto"})

    # o slot ofertado aparece em /slots como qualquer outro horário livre
    slots = client.get(
        "/slots",
        params={
            "service_id": catalogo["servico"]["id"],
            "from": f"{QUARTA}T09:00:00-03:00",
            "to": f"{QUARTA}T12:00:00-03:00",
        },
    ).json()
    assert any(s["inicio"].startswith(f"{QUARTA}T10:00") for s in slots)

    # e um terceiro consegue agendá-lo enquanto a oferta está de pé
    corrida = _agendar(client, catalogo, telefone="+5511977776666")
    assert corrida.status_code == 201


def test_aceite_perdido_devolve_alternativas_e_nao_500(client, catalogo, org_id, canal_fake):
    ap = _agendar(client, catalogo).json()
    ana = _entrar(client, catalogo, nome="Ana", telefone="+5511900000001").json()
    client.post(f"/appointments/{ap['id']}/cancel", json={"motivo": "imprevisto"})

    # alguém tomou o horário antes de Ana responder
    assert _agendar(client, catalogo, telefone="+5511977776666").status_code == 201

    resposta = client.post(f"/waitlist/{ana['id']}/aceitar")
    assert resposta.status_code == 409
    corpo = resposta.json()
    assert corpo["code"] == "SLOT_INDISPONIVEL"
    assert len(corpo["alternativas"]) == 3  # o consolo vem no mesmo payload


def test_aceite_agenda_pelo_mesmo_caminho_de_sempre(client, catalogo, org_id, canal_fake):
    ap = _agendar(client, catalogo).json()
    ana = _entrar(client, catalogo, nome="Ana", telefone="+5511900000001").json()
    client.post(f"/appointments/{ap['id']}/cancel", json={"motivo": "imprevisto"})

    resposta = client.post(f"/waitlist/{ana['id']}/aceitar")
    assert resposta.status_code == 200, resposta.text
    novo = resposta.json()
    assert novo["cliente_nome"] == "Ana"
    assert novo["inicio"] == SLOT
    assert novo["origem"] == "cliente"
    assert "fila de espera" in novo["observacoes"]
    # o compromisso nasceu completo: régua de lembretes e risco calculado
    assert novo["risco_no_show"] is not None

    assert client.get("/waitlist").json() == []  # Ana saiu da fila (aceito)


def test_oferta_expirada_passa_a_vez_ao_proximo(client, catalogo, org_id, canal_fake):
    from app.db import SessionLocal, sessao_org
    from app.jobs import processar_fila
    from app.models import WaitlistEntry
    from app.tempo import agora_utc

    ap = _agendar(client, catalogo).json()
    ana = _entrar(client, catalogo, nome="Ana", telefone="+5511900000001").json()
    _entrar(client, catalogo, nome="Bia", telefone="+5511900000002")
    client.post(f"/appointments/{ap['id']}/cancel", json={"motivo": "imprevisto"})

    # empurra a expiração para o passado, como se os 30 min tivessem passado
    with SessionLocal() as db:
        sessao_org(db, org_id)
        entrada = db.get(WaitlistEntry, ana["id"])
        entrada.expira_em = agora_utc() - timedelta(minutes=1)
        db.commit()

    processar_fila()

    fila = {e["cliente_nome"]: e for e in client.get("/waitlist", params={"incluir_encerrados": True}).json()}
    assert fila["Ana"]["status"] == "expirado"
    assert fila["Bia"]["status"] == "ofertado"  # a vez passou adiante
    assert [e["destinatario"] for e in canal_fake.enviados] == [
        "+5511900000001", "+5511900000002",
    ]


def test_aceitar_oferta_expirada_e_recusado_com_hint(client, catalogo, org_id, canal_fake):
    from app.db import SessionLocal, sessao_org
    from app.models import WaitlistEntry
    from app.tempo import agora_utc

    ap = _agendar(client, catalogo).json()
    ana = _entrar(client, catalogo, nome="Ana", telefone="+5511900000001").json()
    client.post(f"/appointments/{ap['id']}/cancel", json={"motivo": "imprevisto"})

    with SessionLocal() as db:
        sessao_org(db, org_id)
        db.get(WaitlistEntry, ana["id"]).expira_em = agora_utc() - timedelta(minutes=1)
        db.commit()

    resposta = client.post(f"/waitlist/{ana['id']}/aceitar")
    assert resposta.status_code == 409
    assert resposta.json()["code"] == "OFERTA_EXPIRADA"
    assert "/slots" in resposta.json()["hint"]


def test_aceitar_sem_oferta_em_aberto_e_recusado(client, catalogo):
    entrada = _entrar(client, catalogo).json()
    resposta = client.post(f"/waitlist/{entrada['id']}/aceitar")
    assert resposta.status_code == 409
    assert resposta.json()["code"] == "STATUS_INCOMPATIVEL"


def test_optout_nao_recebe_oferta_e_nao_trava_a_fila(client, catalogo, org_id, canal_fake):
    """Quem pediu para sair não é avisado — e não pode bloquear quem está
    atrás dele na fila para sempre."""
    canal_fake.optouts = ["+5511900000001"]
    ap = _agendar(client, catalogo).json()
    _entrar(client, catalogo, nome="Ana", telefone="+5511900000001")
    _entrar(client, catalogo, nome="Bia", telefone="+5511900000002")
    client.post(f"/appointments/{ap['id']}/cancel", json={"motivo": "imprevisto"})

    fila = {e["cliente_nome"]: e for e in client.get("/waitlist").json()}
    assert fila["Ana"]["status"] == "aguardando"  # continua na fila, sem consumir a vez
    assert fila["Bia"]["status"] == "ofertado"    # a oferta seguiu adiante
    assert [e["destinatario"] for e in canal_fake.enviados] == ["+5511900000002"]


def test_entrar_na_fila_avisa_quando_o_cliente_esta_em_optout(client, catalogo, canal_fake):
    canal_fake.optouts = ["+5511900000001"]
    corpo = _entrar(client, catalogo, nome="Ana", telefone="+5511900000001").json()
    assert corpo["avisos"], "o prestador precisa saber que ninguém vai avisar esse cliente"
    assert "NÃO será avisado" in corpo["avisos"][0]


def test_canal_fora_do_ar_nao_queima_a_vez_de_ninguem(client, catalogo, org_id, canal_fake):
    canal_fake.indisponivel = True
    ap = _agendar(client, catalogo).json()
    _entrar(client, catalogo, nome="Ana", telefone="+5511900000001")
    client.post(f"/appointments/{ap['id']}/cancel", json={"motivo": "imprevisto"})

    (ana,) = client.get("/waitlist").json()
    assert ana["status"] == "aguardando"  # volta para a fila intacta
    assert ana["expira_em"] is None
