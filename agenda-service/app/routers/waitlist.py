"""RF-14 — fila de espera: entrar, ver, sair e aceitar a oferta.

O aceite é o ponto delicado: ele NÃO tem atalho. Passa pelo mesmo
`criar_appointment` e pela mesma constraint do banco que qualquer
agendamento — se o slot foi tomado enquanto a mensagem estava no ar, o
cliente recebe as 3 alternativas, como em qualquer conflito (RF-04).
"""

import logging
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.orm import Session

from .. import canal_client, enderecos, fila
from .. import idempotency as idem
from ..auth import ESCOPO_OPERACAO, Credencial, credencial_atual, exigir_escopo
from ..booking import (
    carregar_servico,
    criar_appointment,
    recursos_do_servico,
)
from ..contrato import operacao, respostas
from ..db import get_db
from ..errors import ApiError, NaoEncontrado
from ..models import WaitlistEntry
from ..schemas import AppointmentOut, WaitlistIn, WaitlistOut
from ..tempo import label_humano, local, utc
from .appointments import _e_do_titular, _exigir_titular, _out

log = logging.getLogger("agenda.fila")
router = APIRouter(tags=["fila de espera"])


def _janela_humana(inicio: datetime, fim: datetime) -> str:
    """'quinta, 27 de agosto, 12h às 18h' — a janela como alguém falaria."""
    inteiro = label_humano(inicio)
    return f"{inteiro} às {label_humano(fim).rsplit(', ', 1)[1]}"


def _saida(entrada: WaitlistEntry, posicao: int | None = None, avisos=None) -> WaitlistOut:
    return WaitlistOut(
        id=entrada.id,
        service_id=entrada.service_id,
        resource_id=entrada.resource_id,
        cliente_nome=entrada.cliente_nome,
        cliente_telefone=entrada.cliente_telefone,
        janela_inicio=local(entrada.janela_desejada.lower),
        janela_fim=local(entrada.janela_desejada.upper),
        janela_humana=_janela_humana(
            entrada.janela_desejada.lower, entrada.janela_desejada.upper
        ),
        status=entrada.status,
        posicao=posicao,
        expira_em=entrada.expira_em,
        slot_ofertado=local(entrada.slot_ofertado.lower) if entrada.slot_ofertado else None,
        avisos=avisos or [],
    )


def _em_optout(org_id: UUID, telefone: str) -> bool:
    """Pergunta ao canal se este cliente pediu para sair. Canal fora do ar
    não bloqueia a entrada na fila — a oferta seria recusada lá adiante de
    qualquer forma, que é onde a regra realmente vive (RF-10)."""
    try:
        status, dados = canal_client.chamar("GET", "/canal/optouts", org_id=org_id)
    except canal_client.CanalIndisponivel:
        return False
    if status >= 400 or not isinstance(dados, list):
        return False
    return any(o.get("telefone") == telefone for o in dados)


@router.post(
    "/waitlist",
    response_model=WaitlistOut,
    status_code=201,
    summary="Entra na fila de espera para uma janela de horário",
    description=(
        "Use quando o horário desejado está ocupado (RF-14). A fila é por JANELA "
        "('quinta à tarde'), não por horário exato — para um horário livre, agende "
        "direto. Quando um cancelamento liberar um horário compatível, o primeiro da "
        "fila recebe a oferta pelo canal. **Não há reserva**: o horário segue livre "
        "na grade e quem confirmar primeiro leva. Aceita Idempotency-Key."
    ),
    responses=respostas("NAO_ENCONTRADO", "DATA_SEM_FUSO", "TITULAR_DIVERGENTE"),
    openapi_extra=operacao("agenda:write", idempotente=True),
)
def entrar(
    dados: WaitlistIn,
    request: Request,
    cred: Credencial = Depends(credencial_atual),
    db: Session = Depends(get_db),
):
    exigir_escopo(cred, "agenda:write")
    if repetida := idem.buscar(db, cred.org_id, request, cred.titular):
        return repetida
    telefone = _exigir_titular(cred, dados.cliente_telefone)
    servico = carregar_servico(db, cred.org_id, dados.service_id)
    if dados.resource_id is not None:
        recursos = [r for r in recursos_do_servico(db, servico) if r.id == dados.resource_id]
        if not recursos:
            raise NaoEncontrado("Recurso", str(dados.resource_id))

    entrada = WaitlistEntry(
        org_id=cred.org_id,
        service_id=servico.id,
        resource_id=dados.resource_id,
        cliente_nome=dados.cliente_nome,
        cliente_telefone=telefone,
        janela_desejada=Range(utc(dados.janela_inicio), utc(dados.janela_fim)),
    )
    db.add(entrada)
    db.flush()

    # RF-14/RF-10: quem está em opt-out não recebe oferta automática. A entrada
    # vale (o prestador pode ligar), mas o aviso precisa ser explícito.
    avisos: list[str] = []
    if _em_optout(cred.org_id, telefone):
        avisos.append(
            "Este cliente pediu para não receber mensagens: ele NÃO será avisado "
            "automaticamente quando abrir vaga — combine o contato por outro meio."
        )
        # TODO(etapa de integrações): criar tarefa no tasks-service para o humano.
        log.warning(
            "fila %s: cliente %s em opt-out — oferta automática não sairá",
            entrada.id, telefone,
        )

    corpo = _saida(entrada, posicao=_posicao(db, entrada), avisos=avisos)
    idem.gravar(db, cred.org_id, request, corpo.model_dump(mode="json"), 201, cred.titular)
    db.commit()
    return corpo


def _posicao(db: Session, entrada: WaitlistEntry) -> int | None:
    """Quantos estão na frente, entre os que esperam o mesmo serviço."""
    if entrada.status not in fila.ABERTOS:
        return None
    antes = db.scalars(
        select(WaitlistEntry).where(
            WaitlistEntry.org_id == entrada.org_id,
            WaitlistEntry.service_id == entrada.service_id,
            WaitlistEntry.status.in_(fila.ABERTOS),
            WaitlistEntry.created_at < entrada.created_at,
        )
    ).all()
    return len(antes) + 1


@router.get(
    "/waitlist",
    response_model=list[WaitlistOut],
    summary="Fila de espera da organização, na ordem de chegada",
    description=(
        "Por padrão mostra quem ainda espera (aguardando/ofertado). A posição é por "
        "serviço. **A fila inteira exige `agenda:operacao`** — ela é nome, telefone e "
        "janela desejada de todo mundo que espera, a mesma classe de dado de "
        "`GET /appointments?date=`. Numa sessão de atendimento (`agenda:read` com "
        "titular) a resposta traz apenas a entrada daquele cliente."
    ),
    responses=respostas("CANAL_INDISPONIVEL"),
    openapi_extra=operacao("agenda:read (própria) | agenda:operacao (a fila toda)"),
)
def listar(
    service_id: UUID | None = Query(default=None),
    incluir_encerrados: bool = Query(default=False, description="Também mostra aceito/expirado/cancelado"),
    cred: Credencial = Depends(credencial_atual),
    db: Session = Depends(get_db),
) -> list[WaitlistOut]:
    exigir_escopo(cred, "agenda:read")
    if not cred.titular:
        # Sem titular, "a fila" é a fila de todo mundo: nome, telefone e janela
        # desejada de cada pessoa que espera. É a mesma classe de dado que fez
        # `GET /appointments?date=` exigir `agenda:operacao` — deixá-la em
        # `agenda:read` era uma porta que o isolamento por titular não fechava,
        # porque um bearer de atendimento sem sessão passa por ela.
        exigir_escopo(cred, ESCOPO_OPERACAO)
    fila.expirar_ofertas_vencidas(db, cred.org_id)
    db.commit()

    q = select(WaitlistEntry).where(WaitlistEntry.org_id == cred.org_id)
    if cred.titular:
        # O agente de atendimento recebe só a própria linha — e por vir filtrado
        # da API, o cliente do outro lado não precisa lembrar de filtrar (era
        # esse esquecimento que vazava a fila no fluxo do agente).
        q = q.where(WaitlistEntry.cliente_telefone == enderecos.normalizar(cred.titular))
    if service_id:
        q = q.where(WaitlistEntry.service_id == service_id)
    if not incluir_encerrados:
        q = q.where(WaitlistEntry.status.in_(fila.ABERTOS))
    linhas = db.scalars(q.order_by(WaitlistEntry.created_at)).all()
    return [_saida(e, posicao=_posicao(db, e)) for e in linhas]


def _carregar(db: Session, cred: Credencial, entry_id: UUID) -> WaitlistEntry:
    entrada = db.scalar(
        select(WaitlistEntry).where(
            WaitlistEntry.id == entry_id, WaitlistEntry.org_id == cred.org_id
        )
    )
    if entrada is None or not _e_do_titular(cred, entrada.cliente_telefone):
        raise NaoEncontrado("Entrada da fila", str(entry_id))
    return entrada


@router.delete(
    "/waitlist/{entry_id}",
    response_model=WaitlistOut,
    summary="Sai da fila de espera",
    description="Idempotente: sair de novo devolve o mesmo resultado.",
    responses=respostas("NAO_ENCONTRADO"),
    openapi_extra=operacao("agenda:write"),
)
def sair(
    entry_id: UUID,
    cred: Credencial = Depends(credencial_atual),
    db: Session = Depends(get_db),
):
    exigir_escopo(cred, "agenda:write")
    entrada = _carregar(db, cred, entry_id)
    if entrada.status in fila.ABERTOS:
        entrada.status = "cancelado"
        db.commit()
    return _saida(entrada)


@router.post(
    "/waitlist/{entry_id}/aceitar",
    response_model=AppointmentOut,
    summary="Aceita a oferta e agenda o horário",
    description=(
        "Agenda o slot que a oferta propôs, pela MESMA constraint de qualquer "
        "agendamento — sem caminho privilegiado. Se o horário já foi tomado (não há "
        "reserva durante a oferta), responde 409 SLOT_INDISPONIVEL com as 3 "
        "alternativas mais próximas. Oferta expirada responde 409 OFERTA_EXPIRADA."
    ),
    responses=respostas(
        "NAO_ENCONTRADO", "SLOT_INDISPONIVEL", "OFERTA_EXPIRADA", "STATUS_INCOMPATIVEL"
    ),
    openapi_extra=operacao("agenda:write", idempotente=True),
)
def aceitar(
    entry_id: UUID,
    request: Request,
    cred: Credencial = Depends(credencial_atual),
    db: Session = Depends(get_db),
):
    exigir_escopo(cred, "agenda:write")
    if repetida := idem.buscar(db, cred.org_id, request, cred.titular):
        return repetida
    entrada = _carregar(db, cred, entry_id)

    if entrada.status != "ofertado" or entrada.slot_ofertado is None:
        raise ApiError(
            code="STATUS_INCOMPATIVEL",
            message=f"Esta entrada da fila está '{entrada.status}' — não há oferta para aceitar.",
            hint="Só entradas com oferta em aberto podem ser aceitas. Consulte GET /waitlist.",
            status_code=409,
        )
    if entrada.expira_em and entrada.expira_em <= fila.agora_utc():
        entrada.status = "expirado"
        db.commit()
        raise ApiError(
            code="OFERTA_EXPIRADA",
            message="A janela para aceitar esta oferta já passou.",
            hint=(
                "O horário foi oferecido a quem estava atrás na fila. Entre de novo "
                "com POST /waitlist ou consulte GET /slots para agendar direto."
            ),
            status_code=409,
        )

    servico = carregar_servico(db, cred.org_id, entrada.service_id)
    inicio = entrada.slot_ofertado.lower
    # O recurso é o que a oferta prometeu — não "algum livre agora". Agendar
    # com outro profissional seria entregar coisa diferente do combinado.
    resource_id = entrada.resource_ofertado or entrada.resource_id

    # Sem atalho: mesma função, mesma constraint, mesmo erro com alternativas.
    ap = criar_appointment(
        db, cred.org_id, servico, resource_id, inicio,
        entrada.cliente_nome, entrada.cliente_telefone, origem="cliente",
        observacoes="Veio da fila de espera",
    )
    entrada.status = "aceito"
    corpo = _out(ap, completo=cred.pode(ESCOPO_OPERACAO))
    idem.gravar(db, cred.org_id, request, corpo.model_dump(mode="json"), 200, cred.titular)
    db.commit()
    return corpo
