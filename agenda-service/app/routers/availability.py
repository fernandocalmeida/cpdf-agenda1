from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.orm import Session

from .. import idempotency as idem
from ..auth import Credencial, credencial_atual, exigir_escopo
from ..contrato import operacao, respostas
from ..db import get_db
from ..errors import ApiError, NaoEncontrado
from ..models import AvailabilityBlock, AvailabilityRule, Resource
from ..schemas import (
    BlockIn,
    BlockOut,
    GradeSemanaIn,
    GradeSemanaOut,
    RemocaoBloqueioOut,
    RemocaoRegraOut,
    RuleIn,
    RuleOut,
    RulePatch,
)
from ..tempo import local, utc

router = APIRouter(tags=["grade"])


def _exigir_recurso(db: Session, cred: Credencial, resource_id) -> None:
    if not db.scalar(
        select(Resource).where(Resource.id == resource_id, Resource.org_id == cred.org_id)
    ):
        raise NaoEncontrado("Recurso", str(resource_id))


@router.post(
    "/availability/rules",
    response_model=RuleOut,
    status_code=201,
    summary="Adiciona janela de trabalho semanal a um recurso",
    description="dia_semana: 0=segunda … 6=domingo. Horas em hora local America/Sao_Paulo (RF-02). Aceita Idempotency-Key.",
    responses=respostas("NAO_ENCONTRADO"),
    openapi_extra=operacao("agenda:admin", idempotente=True),
)
def criar_rule(
    dados: RuleIn,
    request: Request,
    cred: Credencial = Depends(credencial_atual),
    db: Session = Depends(get_db),
):
    exigir_escopo(cred, "agenda:admin")
    _exigir_recurso(db, cred, dados.resource_id)
    if repetida := idem.buscar(db, cred.org_id, request, cred.titular):
        return repetida
    regra = AvailabilityRule(org_id=cred.org_id, **dados.model_dump())
    db.add(regra)
    db.flush()
    corpo = RuleOut.model_validate(regra)
    idem.gravar(db, cred.org_id, request, corpo.model_dump(mode="json"), 201, cred.titular)
    db.commit()
    return corpo


@router.get(
    "/availability/rules",
    response_model=list[RuleOut],
    summary="Grade semanal da organização",
    responses=respostas(),
    openapi_extra=operacao("agenda:read"),
)
def listar_rules(
    resource_id: UUID | None = Query(default=None, description="Só a grade deste recurso"),
    cred: Credencial = Depends(credencial_atual),
    db: Session = Depends(get_db),
) -> list[RuleOut]:
    exigir_escopo(cred, "agenda:read")
    q = select(AvailabilityRule).where(AvailabilityRule.org_id == cred.org_id)
    if resource_id:
        q = q.where(AvailabilityRule.resource_id == resource_id)
    linhas = db.scalars(
        q.order_by(AvailabilityRule.dia_semana, AvailabilityRule.hora_inicio)
    ).all()
    return [RuleOut.model_validate(r) for r in linhas]


@router.put(
    "/availability/rules",
    response_model=GradeSemanaOut,
    summary="Define a semana inteira de um recurso (substitui a grade)",
    description=(
        "Declarativo: descreva a semana **como ela deve ficar** e o servidor faz a "
        "diferença numa transação só. Substitui todas as janelas do recurso — "
        "`janelas: []` limpa a grade. Prefira isto a remendar janela a janela: "
        "listar, remover uma, criar duas e esquecer nenhuma é onde um agente erra. "
        "Aceita Idempotency-Key."
    ),
    responses=respostas("NAO_ENCONTRADO", "PERIODO_INVALIDO"),
    openapi_extra=operacao("agenda:admin", idempotente=True),
)
def definir_grade(
    dados: GradeSemanaIn,
    request: Request,
    resource_id: UUID = Query(description="Recurso cuja semana está sendo definida"),
    cred: Credencial = Depends(credencial_atual),
    db: Session = Depends(get_db),
):
    exigir_escopo(cred, "agenda:admin")
    _exigir_recurso(db, cred, resource_id)
    if repetida := idem.buscar(db, cred.org_id, request, cred.titular):
        return repetida

    for janela in dados.janelas:
        if janela.hora_fim <= janela.hora_inicio:
            raise ApiError(
                code="PERIODO_INVALIDO",
                message=(
                    f"A janela de {janela.hora_inicio}–{janela.hora_fim} termina antes "
                    "de começar."
                ),
                hint="Confira hora_inicio e hora_fim de cada janela.",
            )
    # Sobreposição no mesmo dia quase sempre é engano (09–12 e 11–14). O motor
    # de slots faria a união e o erro passaria despercebido — melhor recusar
    # com o conflito nomeado do que aceitar uma grade que ninguém quis.
    por_dia: dict[int, list] = {}
    for janela in sorted(dados.janelas, key=lambda j: (j.dia_semana, j.hora_inicio)):
        anteriores = por_dia.setdefault(janela.dia_semana, [])
        if anteriores and janela.hora_inicio < anteriores[-1].hora_fim:
            raise ApiError(
                code="PERIODO_INVALIDO",
                message=(
                    f"Duas janelas do dia {janela.dia_semana} se sobrepõem: "
                    f"{anteriores[-1].hora_inicio}–{anteriores[-1].hora_fim} e "
                    f"{janela.hora_inicio}–{janela.hora_fim}."
                ),
                hint="Junte as duas numa só ou ajuste os horários para não colidirem.",
            )
        anteriores.append(janela)

    # Tudo na mesma transação: ou a semana nova entra inteira, ou a antiga fica.
    removidas = db.execute(
        delete(AvailabilityRule).where(
            AvailabilityRule.org_id == cred.org_id,
            AvailabilityRule.resource_id == resource_id,
        )
    ).rowcount
    novas = [
        AvailabilityRule(
            org_id=cred.org_id,
            resource_id=resource_id,
            dia_semana=j.dia_semana,
            hora_inicio=j.hora_inicio,
            hora_fim=j.hora_fim,
        )
        for j in sorted(dados.janelas, key=lambda j: (j.dia_semana, j.hora_inicio))
    ]
    db.add_all(novas)
    db.flush()

    corpo = GradeSemanaOut(
        resource_id=resource_id,
        janelas=[RuleOut.model_validate(r) for r in novas],
        removidas=removidas,
    )
    idem.gravar(db, cred.org_id, request, corpo.model_dump(mode="json"), 200, cred.titular)
    db.commit()
    return corpo


@router.patch(
    "/availability/rules/{rule_id}",
    response_model=RuleOut,
    summary="Altera uma janela da grade semanal",
    description="Só os campos enviados mudam. A alteração vale para os slots futuros na hora.",
    responses=respostas("NAO_ENCONTRADO", "PERIODO_INVALIDO"),
    openapi_extra=operacao("agenda:admin"),
)
def alterar_rule(
    rule_id: UUID,
    dados: RulePatch,
    cred: Credencial = Depends(credencial_atual),
    db: Session = Depends(get_db),
):
    exigir_escopo(cred, "agenda:admin")
    regra = db.scalar(
        select(AvailabilityRule).where(
            AvailabilityRule.id == rule_id, AvailabilityRule.org_id == cred.org_id
        )
    )
    if regra is None:
        raise NaoEncontrado("Janela da grade", str(rule_id))
    for campo, valor in dados.model_dump(exclude_unset=True).items():
        setattr(regra, campo, valor)
    if regra.hora_fim <= regra.hora_inicio:
        raise ApiError(
            code="PERIODO_INVALIDO",
            message="O fim da janela precisa ser depois do início.",
            hint="Confira hora_inicio e hora_fim após a alteração.",
        )
    db.commit()
    return RuleOut.model_validate(regra)


@router.delete(
    "/availability/rules/{rule_id}",
    summary="Remove uma janela da grade semanal",
    description=(
        "Exclusão real: grade é configuração, não histórico. O motor de slots deixa "
        "de oferecer os horários desta janela imediatamente; agendamentos já feitos "
        "não mudam. Idempotente: remover de novo devolve o mesmo resultado."
    ),
    response_model=RemocaoRegraOut,
    responses=respostas(),
    openapi_extra=operacao("agenda:admin"),
)
def remover_rule(
    rule_id: UUID,
    cred: Credencial = Depends(credencial_atual),
    db: Session = Depends(get_db),
) -> dict:
    exigir_escopo(cred, "agenda:admin")
    removidas = db.execute(
        delete(AvailabilityRule).where(
            AvailabilityRule.id == rule_id, AvailabilityRule.org_id == cred.org_id
        )
    ).rowcount
    db.commit()
    return {"id": str(rule_id), "removida": bool(removidas)}


@router.delete(
    "/availability/blocks/{block_id}",
    summary="Remove um bloqueio pontual",
    description=(
        "Exclusão real: os horários do período voltam a ser ofertados na hora. "
        "Idempotente."
    ),
    response_model=RemocaoBloqueioOut,
    responses=respostas(),
    openapi_extra=operacao("agenda:admin"),
)
def remover_block(
    block_id: UUID,
    cred: Credencial = Depends(credencial_atual),
    db: Session = Depends(get_db),
) -> dict:
    exigir_escopo(cred, "agenda:admin")
    removidos = db.execute(
        delete(AvailabilityBlock).where(
            AvailabilityBlock.id == block_id, AvailabilityBlock.org_id == cred.org_id
        )
    ).rowcount
    db.commit()
    return {"id": str(block_id), "removido": bool(removidos)}


@router.get(
    "/availability/blocks",
    response_model=list[BlockOut],
    summary="Bloqueios pontuais vigentes ou futuros",
    description=(
        "Lista bloqueios cujo fim ainda não passou. Filtre por recurso se quiser. "
        "Exige `agenda:operacao`: o motivo do bloqueio é nota interna do prestador "
        "('cirurgia', 'férias'), não informação de atendimento."
    ),
    responses=respostas(),
    openapi_extra=operacao("agenda:operacao"),
)
def listar_blocks(
    resource_id: UUID | None = None,
    cred: Credencial = Depends(credencial_atual),
    db: Session = Depends(get_db),
) -> list[BlockOut]:
    exigir_escopo(cred, "agenda:operacao")
    q = select(AvailabilityBlock).where(
        AvailabilityBlock.org_id == cred.org_id,
        text("upper(periodo) >= now()"),
    )
    if resource_id:
        q = q.where(AvailabilityBlock.resource_id == resource_id)
    return [
        BlockOut(
            id=b.id,
            resource_id=b.resource_id,
            inicio=local(b.periodo.lower),
            fim=local(b.periodo.upper),
            motivo=b.motivo,
        )
        for b in db.scalars(q.order_by(text("lower(periodo)")))
    ]


@router.post(
    "/availability/blocks",
    response_model=BlockOut,
    status_code=201,
    summary="Bloqueia um período pontual (feriado, almoço, férias)",
    description="Início e fim em ISO 8601 com offset. O motivo aparece na agenda do dia. Aceita Idempotency-Key.",
    responses=respostas("NAO_ENCONTRADO", "PERIODO_INVALIDO", "DATA_SEM_FUSO"),
    openapi_extra=operacao("agenda:admin", idempotente=True),
)
def criar_block(
    dados: BlockIn,
    request: Request,
    cred: Credencial = Depends(credencial_atual),
    db: Session = Depends(get_db),
):
    exigir_escopo(cred, "agenda:admin")
    _exigir_recurso(db, cred, dados.resource_id)
    if dados.fim <= dados.inicio:
        raise ApiError(
            code="PERIODO_INVALIDO",
            message="O fim do bloqueio precisa ser depois do início.",
            hint="Inverta os valores ou confira o offset de fuso.",
        )
    if repetida := idem.buscar(db, cred.org_id, request, cred.titular):
        return repetida
    bloco = AvailabilityBlock(
        org_id=cred.org_id,
        resource_id=dados.resource_id,
        periodo=Range(utc(dados.inicio), utc(dados.fim)),
        motivo=dados.motivo,
    )
    db.add(bloco)
    db.flush()
    corpo = BlockOut(
        id=bloco.id,
        resource_id=bloco.resource_id,
        inicio=local(dados.inicio),
        fim=local(dados.fim),
        motivo=bloco.motivo,
    )
    idem.gravar(db, cred.org_id, request, corpo.model_dump(mode="json"), 201, cred.titular)
    db.commit()
    return corpo
