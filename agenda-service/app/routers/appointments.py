from datetime import date, datetime, timedelta
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import confirmacao, enderecos, ofertas
from .. import idempotency as idem
from ..auth import ESCOPO_OPERACAO, Credencial, credencial_atual, exigir_escopo
from ..booking import (
    _evento,
    _historico,
    carregar_servico,
    criar_appointment,
    reagendar,
    recursos_do_servico,
)
from ..contrato import operacao, respostas
from ..db import get_db
from ..errors import ApiError, NaoEncontrado
from ..models import Appointment, AppointmentHistory
from ..schemas import (
    AgendaDiaOut,
    AppointmentIn,
    AppointmentOut,
    CancelIn,
    HistoricoOut,
    RescheduleIn,
)
from ..tempo import TZ, label_humano, local

router = APIRouter(tags=["agendamentos"])


def _out(ap: Appointment, completo: bool = True) -> AppointmentOut:
    """`completo=False` esconde risco de falta e observações.

    Não é redundância com a guarda de titular: quem tem só atendimento passa
    na guarda (o compromisso É do cliente dele) e ainda assim não deve
    receber esses campos. São dados *sobre* o cliente, produzidos pela
    operação — um bot dizendo "você é risco alto de faltar", ou lendo em voz
    alta a observação "cliente difícil", é dano que a posse do registro não
    autoriza.
    """
    return AppointmentOut(
        id=ap.id,
        service_id=ap.service_id,
        resource_id=ap.resource_id,
        cliente_nome=ap.cliente_nome,
        cliente_telefone=ap.cliente_telefone,
        inicio=local(ap.periodo.lower),
        fim=local(ap.periodo.upper),
        label_humano=label_humano(ap.periodo.lower),
        status=ap.status,
        origem=ap.origem,
        risco_no_show=ap.risco_no_show if completo else None,
        risco_detalhe=ap.risco_detalhe if completo else None,
        observacoes=ap.observacoes if completo else None,
        series_id=ap.series_id,
    )


def _carregar(db: Session, cred: Credencial, appointment_id: UUID) -> Appointment:
    ap = db.scalar(
        select(Appointment).where(
            Appointment.id == appointment_id, Appointment.org_id == cred.org_id
        )
    )
    if ap is None or not _e_do_titular(cred, ap.cliente_telefone):
        # 404, não 403: responder "existe, mas não é seu" confirmaria a
        # existência do compromisso — e com id sequencial ou adivinhado isso
        # já é vazamento. Para quem não é dono, o registro simplesmente não há.
        raise NaoEncontrado("Compromisso", str(appointment_id))
    return ap


def _e_do_titular(cred: Credencial, telefone: str) -> bool:
    """Sem titular, a credencial é da organização e alcança tudo dela."""
    return cred.titular is None or enderecos.mesmo(telefone, cred.titular)


def _exigir_titular(cred: Credencial, telefone: str) -> str:
    """Endereço normalizado para gravar, recusando escrita em nome de terceiro."""
    if not _e_do_titular(cred, telefone):
        raise ApiError(
            code="TITULAR_DIVERGENTE",
            message="Esta sessão de atendimento não fala pelo cliente informado.",
            hint=(
                "O token de sessão é cunhado pelo canal para o cliente que "
                "escreveu. Omita `cliente_telefone` ou use o endereço da conversa."
            ),
            status_code=403,
        )
    return enderecos.normalizar(telefone)


@router.post(
    "/appointments",
    response_model=AppointmentOut,
    status_code=201,
    summary="Agenda um horário",
    description=(
        "Exige serviço + horário + cliente identificado (nome e telefone) — RF-03. "
        "Sem resource_id, usa o primeiro recurso do serviço com o slot livre. "
        "Horário ocupado responde 409 SLOT_INDISPONIVEL com as 3 alternativas mais "
        "próximas já no payload. Aceita Idempotency-Key (obrigatório para agentes)."
    ),
    responses=respostas("NAO_ENCONTRADO", "SLOT_INDISPONIVEL", "DATA_SEM_FUSO", "TITULAR_DIVERGENTE"),
    openapi_extra=operacao("agenda:write", idempotente=True),
)
def agendar(
    dados: AppointmentIn,
    request: Request,
    cred: Credencial = Depends(credencial_atual),
    db: Session = Depends(get_db),
):
    exigir_escopo(cred, "agenda:write")
    if repetida := idem.buscar(db, cred.org_id, request, cred.titular):
        return repetida
    telefone = _exigir_titular(cred, dados.cliente_telefone)
    servico = carregar_servico(db, cred.org_id, dados.service_id)
    recursos = recursos_do_servico(db, servico)
    if dados.resource_id is not None:
        recursos = [r for r in recursos if r.id == dados.resource_id]
    if not recursos:
        raise NaoEncontrado("Recurso", str(dados.resource_id))
    ap = None
    ultimo_erro: ApiError | None = None
    for recurso in recursos:
        try:
            ap = criar_appointment(
                db,
                cred.org_id,
                servico,
                recurso.id,
                dados.inicio,
                dados.cliente_nome,
                telefone,
                dados.origem,
                dados.observacoes,
            )
            break
        except ApiError as e:
            if e.code != "SLOT_INDISPONIVEL":
                raise
            ultimo_erro = e
    if ap is None:
        raise ultimo_erro or ApiError(
            code="SLOT_INDISPONIVEL",
            message="Nenhum recurso do serviço está livre neste horário.",
            hint="Consulte GET /slots e ofereça as alternativas ao cliente.",
            status_code=409,
        )
    corpo = _out(ap, completo=cred.pode(ESCOPO_OPERACAO))
    idem.gravar(db, cred.org_id, request, corpo.model_dump(mode="json"), 201, cred.titular)
    db.commit()
    return corpo


@router.post(
    "/appointments/{appointment_id}/reschedule",
    response_model=AppointmentOut,
    summary="Reagenda de forma atômica",
    description=(
        "Ou o novo horário é reservado e o antigo liberado na mesma transação, ou nada "
        "muda (RF-06). Conflito responde 409 com alternativas. O compromisso volta ao "
        "status 'agendado' — a confirmação anterior não vale para o novo horário."
    ),
    responses=respostas(
        "NAO_ENCONTRADO", "SLOT_INDISPONIVEL", "STATUS_INCOMPATIVEL", "DATA_SEM_FUSO"
    ),
    openapi_extra=operacao("agenda:write", idempotente=True),
)
def reagendar_endpoint(
    appointment_id: UUID,
    dados: RescheduleIn,
    request: Request,
    cred: Credencial = Depends(credencial_atual),
    db: Session = Depends(get_db),
):
    exigir_escopo(cred, "agenda:write")
    if repetida := idem.buscar(db, cred.org_id, request, cred.titular):
        return repetida
    ap = _carregar(db, cred, appointment_id)
    if ap.status in ("cancelado", "realizado", "no_show"):
        raise ApiError(
            code="STATUS_INCOMPATIVEL",
            message=f"Compromisso está '{ap.status}' — não dá para reagendar.",
            hint="Crie um novo agendamento com POST /appointments.",
            status_code=409,
        )
    servico = carregar_servico(db, cred.org_id, ap.service_id)
    ap = reagendar(db, ap, servico, dados.novo_inicio, cred.ator, dados.motivo)
    corpo = _out(ap, completo=cred.pode(ESCOPO_OPERACAO))
    idem.gravar(db, cred.org_id, request, corpo.model_dump(mode="json"), 200, cred.titular)
    db.commit()
    return corpo


@router.post(
    "/appointments/{appointment_id}/cancel",
    response_model=AppointmentOut,
    summary="Cancela e libera o slot imediatamente",
    description=(
        "Exige escopo agenda:cancel. Disparado por agente, exige confirmação humana: a "
        "primeira chamada (sem confirmation_token) devolve 409 CONFIRMACAO_NECESSARIA "
        "com a prévia e o token; repita com o token após o humano aprovar (expira em 5 "
        "min). O slot volta para a grade na hora — a fila de espera (RF-14) será "
        "notificada quando a etapa 7 ligar o job."
    ),
    responses=respostas(
        "NAO_ENCONTRADO",
        "CONFIRMACAO_NECESSARIA",
        "CONFIRMACAO_INVALIDA",
        "CONFIRMACAO_EXPIRADA",
    ),
    openapi_extra=operacao("agenda:cancel", idempotente=True),
)
def cancelar(
    appointment_id: UUID,
    dados: CancelIn,
    request: Request,
    background: BackgroundTasks,
    cred: Credencial = Depends(credencial_atual),
    db: Session = Depends(get_db),
):
    exigir_escopo(cred, "agenda:cancel")
    if repetida := idem.buscar(db, cred.org_id, request, cred.titular):
        return repetida
    ap = _carregar(db, cred, appointment_id)
    if ap.status == "cancelado":
        return _out(ap, completo=cred.pode(ESCOPO_OPERACAO))  # idempotente por natureza
    if cred.ator == "agente":
        if dados.confirmation_token is None:
            raise ApiError(
                code="CONFIRMACAO_NECESSARIA",
                message="Cancelamento é irreversível e exige confirmação humana.",
                hint=(
                    "Mostre a prévia ao humano e, com o OK, repita esta chamada com o "
                    "confirmation_token do payload."
                ),
                status_code=409,
                extra={
                    "previa": {
                        "compromisso": str(ap.id),
                        "cliente": ap.cliente_nome,
                        "horario": label_humano(ap.periodo.lower),
                    },
                    "confirmation_token": confirmacao.gerar_token("cancel", ap.id),
                },
            )
        confirmacao.validar_token(dados.confirmation_token, "cancel", ap.id)
    anterior = ap.periodo
    ap.status = "cancelado"
    _historico(db, ap, "cancelado", de=anterior, origem=cred.ator, motivo=dados.motivo)
    _evento(db, ap, "agenda.appointment.canceled")
    corpo = _out(ap, completo=cred.pode(ESCOPO_OPERACAO))
    idem.gravar(db, cred.org_id, request, corpo.model_dump(mode="json"), 200, cred.titular)
    db.commit()
    # RF-14: o horário voltou para a grade — quem está na fila é avisado logo
    # depois da resposta, não durante (o cancelamento não espera o WhatsApp).
    background.add_task(
        ofertas.ofertar_slot_liberado,
        cred.org_id, ap.service_id, ap.resource_id, anterior.lower, anterior.upper,
    )
    return corpo


@router.post(
    "/appointments/{appointment_id}/confirm",
    response_model=AppointmentOut,
    summary="Marca o compromisso como confirmado pelo cliente",
    description="Use quando o cliente responder 'sim' à confirmação. Só compromissos 'agendado' aceitam.",
    responses=respostas("NAO_ENCONTRADO", "STATUS_INCOMPATIVEL"),
    openapi_extra=operacao("agenda:write"),
)
def confirmar(
    appointment_id: UUID,
    cred: Credencial = Depends(credencial_atual),
    db: Session = Depends(get_db),
):
    exigir_escopo(cred, "agenda:write")
    ap = _carregar(db, cred, appointment_id)
    if ap.status != "agendado":
        raise ApiError(
            code="STATUS_INCOMPATIVEL",
            message=f"Compromisso está '{ap.status}' — só 'agendado' pode ser confirmado.",
            hint="Nada a fazer se já está confirmado; senão, verifique o compromisso certo.",
            status_code=409,
        )
    ap.status = "confirmado"
    _historico(db, ap, "confirmado", origem=cred.ator)
    db.commit()
    return _out(ap, completo=cred.pode(ESCOPO_OPERACAO))


@router.post(
    "/appointments/{appointment_id}/no-show",
    response_model=AppointmentOut,
    summary="Registra falta do cliente (alimenta o histórico de risco — IA-03)",
    description=(
        "Exige `agenda:operacao`: registrar falta é ato do operador e alimenta o "
        "risco de no-show do cliente. Um agente de atendimento nunca marca falta."
    ),
    responses=respostas("NAO_ENCONTRADO"),
    openapi_extra=operacao("agenda:operacao"),
)
def no_show(
    appointment_id: UUID,
    cred: Credencial = Depends(credencial_atual),
    db: Session = Depends(get_db),
):
    exigir_escopo(cred, "agenda:operacao")
    ap = _carregar(db, cred, appointment_id)
    ap.status = "no_show"
    _historico(db, ap, "no_show", origem=cred.ator)
    db.commit()
    return _out(ap, completo=cred.pode(ESCOPO_OPERACAO))


@router.get(
    "/appointments",
    response_model=list[AppointmentOut],
    summary="Lista compromissos por data e recurso",
    description=(
        "Todos os status, na ordem do dia. Exige `agenda:operacao` — devolve nome, "
        "contato e observações de **todos** os clientes daquele dia. Para checar "
        "disponibilidade use GET /slots; para o compromisso de um cliente, "
        "GET /appointments/proximo."
    ),
    responses=respostas(),
    openapi_extra=operacao("agenda:operacao"),
)
def listar(
    data: date = Query(alias="date"),
    resource_id: UUID | None = Query(default=None, alias="resource"),
    cred: Credencial = Depends(credencial_atual),
    db: Session = Depends(get_db),
) -> list[AppointmentOut]:
    exigir_escopo(cred, "agenda:operacao")
    dia_ini = datetime.combine(data, datetime.min.time(), tzinfo=TZ)
    dia_fim = dia_ini + timedelta(days=1)
    from sqlalchemy.dialects.postgresql import Range

    q = select(Appointment).where(
        Appointment.org_id == cred.org_id,
        Appointment.periodo.overlaps(Range(dia_ini, dia_fim)),
    )
    if resource_id:
        q = q.where(Appointment.resource_id == resource_id)
    return [_out(a) for a in db.scalars(q.order_by(Appointment.periodo))]  # exige operacao


@router.get(
    "/appointments/proximo",
    response_model=AppointmentOut,
    summary="Próximo compromisso futuro de um telefone",
    description=(
        "O ponto de partida do agente quando o cliente escreve: acha o compromisso "
        "('agendado' ou 'confirmado') mais próximo no futuro para este telefone. "
        "404 se não houver — nesse caso, ofereça um novo agendamento."
    ),
    responses=respostas("NAO_ENCONTRADO"),
    openapi_extra=operacao("agenda:read"),
)
def proximo(
    telefone: str = Query(
        min_length=3,
        description="Endereço do cliente no canal: +5511998765432 (WhatsApp) ou tg:123456789 (Telegram)",
    ),
    cred: Credencial = Depends(credencial_atual),
    db: Session = Depends(get_db),
):
    exigir_escopo(cred, "agenda:read")
    from sqlalchemy import text as sql_text

    # Numa sessão de atendimento o parâmetro é ignorado: quem responde é o
    # titular provado pelo canal. Sem isso a rota vira enumeração — um
    # telefone por chamada, e o agente descobre a agenda inteira.
    alvo = enderecos.normalizar(cred.titular or telefone)
    ap = db.scalars(
        select(Appointment)
        .where(
            Appointment.org_id == cred.org_id,
            Appointment.cliente_telefone == alvo,
            Appointment.status.in_(("agendado", "confirmado")),
            sql_text("lower(periodo) > now()"),
        )
        .order_by(Appointment.periodo)
        .limit(1)
    ).first()
    if ap is None:
        raise NaoEncontrado("Compromisso futuro do telefone", alvo)
    return _out(ap, completo=cred.pode(ESCOPO_OPERACAO))


@router.get(
    "/agenda/day",
    response_model=AgendaDiaOut,
    summary="Agenda do dia, narrada para o agente",
    description=(
        "Visão consolidada em linguagem clara: compromissos com status e risco, na "
        "ordem do dia. Use para responder 'como está meu dia?' — não para checar "
        "disponibilidade (use GET /slots). Exige `agenda:operacao`: a narrativa "
        "nomeia todos os clientes do dia."
    ),
    responses=respostas(),
    openapi_extra=operacao("agenda:operacao"),
)
def agenda_do_dia(
    data: date = Query(alias="date"),
    cred: Credencial = Depends(credencial_atual),
    db: Session = Depends(get_db),
) -> dict:
    exigir_escopo(cred, "agenda:operacao")
    compromissos = listar(data=data, resource_id=None, cred=cred, db=db)
    linhas = [
        f"{c.label_humano} — {c.cliente_nome} ({c.status}"
        + (f", risco de falta {c.risco_no_show}" if c.risco_no_show else "")
        + ")"
        for c in compromissos
    ]
    return {
        "data": data.isoformat(),
        "total": len(compromissos),
        "narrativa": "\n".join(linhas) or "Nenhum compromisso neste dia.",
        "compromissos": [c.model_dump(mode="json") for c in compromissos],
    }


@router.get(
    "/appointments/{appointment_id}/history",
    response_model=list[HistoricoOut],
    summary="Histórico de alterações do compromisso (quem, quando, por quê)",
    responses=respostas("NAO_ENCONTRADO"),
    openapi_extra=operacao("agenda:read"),
)
def historico(
    appointment_id: UUID,
    cred: Credencial = Depends(credencial_atual),
    db: Session = Depends(get_db),
) -> list[dict]:
    exigir_escopo(cred, "agenda:read")
    _carregar(db, cred, appointment_id)  # 404 se não é da org
    linhas = db.scalars(
        select(AppointmentHistory)
        .where(AppointmentHistory.appointment_id == appointment_id)
        .order_by(AppointmentHistory.em)
    ).all()
    return [
        {
            "acao": h.acao,
            "de": h.de.lower.isoformat() if h.de else None,
            "para": h.para.lower.isoformat() if h.para else None,
            "origem": h.origem,
            "motivo": h.motivo,
            "em": h.em.isoformat(),
        }
        for h in linhas
    ]
