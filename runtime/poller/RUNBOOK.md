# Runbook — ambiente reproduzível do poller (LOL-79 / card t_0d5d2c4c)

Escopo: escolher **um único** interpreter/entrypoint que importe, no mesmo processo,
Hermes, `hermes-agent-runtime` e a API Kanban, executável **fora de shell de worker
cercado**. Preparação/documentação apenas: nenhum restart, migration, deploy, push,
merge ou alteração de secrets foi executado. Depende de LOL-78 (cadeia versionada no
commit `dc3f7df`, aprovada por QA `t_e800924c` e review `t_32271fcc`).

> **Atualização LOL-97 / t_a4d0e749 (unit permanente, follow-up pós-merge):** a cadeia foi
> publicada e mergeada em `origin/main` @ **`ade1218`** (PR #5). O entrypoint foi estabilizado
> (§10): interpreter resolvido por `facts.json` (sobrevive a `hermes update`), cadeia fixada no
> commit mergeado extraído read-only em `~/.hermes/runtime/chain/ade1218` (sem dependência do
> worktree volátil), `HERMES_HOME` explícito e uma unit systemd permanente (service + timer),
> preparada mas **não habilitada/iniciada**. As seções §1–§9 abaixo são o registro histórico da
> LOL-79 e permanecem válidas; o canário read-only é `run_poller.sh --canary`.

Host `vmi3571330`, 2026-10-03, Linux 6.8.0-139-generic.

---

## 1. Decisão

**Interpreter (único):**

    /home/alexandre/.hermes/installs/974d540f82c0c176/environments/496d61b5a8854999ba0e681f1ec5b777/venv/bin/python   (Python 3.14.7)

**Entrypoint (único):** o wrapper `run_poller.sh --probe | <module-or-script.py> [args...]`
(em `~/.hermes/runtime/poller/run_poller.sh`), que monta o ambiente e faz `exec` no
interpreter acima.

### Por que este interpreter

Matriz de candidatos (evidência: `interpreter_matrix.out`, `probe_readonly.out`):

| Candidato | Python | Importa Hermes+Kanban | Importa hermes_agent_runtime | Veredito |
|---|---|---|---|---|
| `hermes-agent/venv` (venv compartilhado) | 3.11.16 | OK só com `PYTHONPATH=hermes-agent` | OK só com `PYTHONPATH=chain/src` | rejeitado: 3.11 (divergente de `.python-version`=3.14), editable Hermes defasado (0.21.2) e runtime nativo do venv é o **pin antigo** `d00f3b79` (sem poller/selector) |
| launcher/tools python + `PYTHONPATH` | 3.14.7 | **FAIL** `No module named 'ruamel'` | OK | rejeitado: sem as deps; o launcher real só funciona porque `hermes_bootstrap` ativa as deps do PM-venv |
| **PM-venv do install (`496d…`) + `PYTHONPATH`** | **3.14.7** | **OK** | **OK** | **escolhido** |

O PM-venv é exatamente o ambiente que `hermes_bootstrap.activate_dependencies()`
seleciona para este install (`facts.json → packages.venv.environment`), ou seja, carrega
o mesmo conjunto de site-packages que o gateway/orquestrador em execução usam — e casa
com `.python-version` (3.14). O pacote `hermes_agent_runtime` é stdlib-only, então não
precisa de `pip`/instalação: basta uma entrada de `PYTHONPATH`.

### Binding do código Hermes

`PYTHONPATH` do wrapper põe o **checkout vivo** `/home/alexandre/.hermes/hermes-agent`
**antes** da cadeia, para que `hermes_cli.kanban_db` venha da mesma árvore que o gateway
importa. Observação relevante (LOL-79): a cópia da árvore dentro do PM-workspace **não é
byte-idêntica** ao checkout vivo em `hermes_cli/kanban_db.py`
(`live=8226cb30…` vs `pm_ws=cc30f2a5…`); por isso o checkout vivo é fixado primeiro.
`hermes_yaml.py`, `cli.py` e `kanban_db_dispatch.py` são idênticos.

---

## 2. Comando

Probe read-only (seguro em qualquer contexto, inclusive cercado):

    sh /home/alexandre/.hermes/runtime/poller/run_poller.sh --probe

Poller real (placeholder de módulo; a implementação concreta é da correção 3):

    sh /home/alexandre/.hermes/runtime/poller/run_poller.sh <module-or-script.py> [args...]

O wrapper exporta:

    PYTHONPATH="$HERMES_CHECKOUT:$CHAIN_SRC${PYTHONPATH:+:$PYTHONPATH}"
    HERMES_KANBAN_BOARD="lolcoach"     # overridable

e faz `exec` no interpreter. Variáveis `POLLER_PYTHON`, `HERMES_CHECKOUT`, `CHAIN_SRC`
permitem override para teste.

Resolução do interpreter após um `hermes update` (o id de geração pode mudar):

    /usr/bin/python3 -c "import json;print(json.load(open('/home/alexandre/.hermes/installs/974d540f82c0c176/facts.json'))['packages']['venv']['environment'])"

---

## 3. Contexto de execução — fora de shell de worker cercado

Um `delegate_task` child carrega `HERMES_DELEGATED_CHILD_CONTEXT` e
`hermes_cli.kanban_db._assert_not_delegated_child_mutation` recusa mutação de board
(achado de `t_6070f37a` §7). Portanto o poller mutante **não pode** rodar como shell de
um worker Kanban.

O wrapper aplica um guard: se `HERMES_DELEGATED_CHILD_CONTEXT` estiver setado, o poller
muta­nte recusa iniciar (`exit 3`) com mensagem explícita. O `--probe` (read-only) não é
bloqueado. Locais válidos: processo do gateway/orquestrador, ou **systemd user unit /
cron fora do Hermes** (não é necessário estar no processo do gateway — o próprio
`dispatch_once` do dispatcher instancia os workers).

Prova do guard (evidência `probe_readonly.out`, bloco "CONFINEMENT GUARD"): no shell
atual (cercado) o wrapper retornou `exit 3`; o `--probe` retornou `RESULT: PASS`.

---

## 4. Probe read-only

`probe_env.py` (em `~/.hermes/runtime/poller/`) imprime interpreter/versão, caminhos dos
módulos importados, commit e `sha256[:16]` dos arquivos da cadeia, os símbolos exigidos
(`RuntimeOrchestrator`, `LinearIssuePoller`, `LinearIssueSelector`,
`RuntimeDispatchAdapter`) e uma leitura real do board. Somente leitura: `get_task` +
`count(*)`, nunca cria/dispatcha/muta/arquiva; não lê credenciais.

Resultado real (evidência `probe_readonly.out`):

    RESULT: PASS
    chain_commit: dc3f7dfcc733caa167e2173ab5fbe4da1d680b78   (== expected)
    board_read: board=lolcoach tasks=162 t_0d5d2c4c=running
    portas 12/12 imports OK (hermes_cli, kanban_db[_connect|_dispatch],
    hermes_agent_runtime[.orchestrator|.poller|.selector|.adapter|.kanban|.payloads|.supabase])

A suíte da própria cadeia também roda neste interpreter:
`PYTHONPATH=chain/src <PM-venv-python> -m unittest discover -s tests` → **27 tests OK**.

---

## 5. Secrets / ambiente (para a correção 3)

O probe não lê credenciais. O poller real (`SupabaseStore.from_environment()`) precisa
de `SUPABASE_URL`/`SUPABASE_SERVICE_ROLE_KEY` (e o cliente Linear) no processo. Essas
variáveis vivem em `~/.hermes/shared/.env` (modo 600) e devem ser carregadas **no
processo do entrypoint**, não por expansão de shell na linha de comando. Nenhum secret
foi alterado, copiado ou impresso nesta tarefa.

---

## 6. Rollback

Tudo é aditivo e reversível; nenhum estado funcional foi alterado.

    # Desfazer o entrypoint/ambiente:
    rm -rf /home/alexandre/.hermes/runtime/poller
    rm -rf /home/alexandre/.hermes/runtime/evidence/t_0d5d2c4c

Não há instalação a remover (o pacote entra por `PYTHONPATH`, não por `pip`), nenhum
serviço criado/habilitado, nenhum arquivo do repositório `hermes-agent-runtime` alterado,
e nenhum restart executado. O checkout vivo e o PM-venv não foram modificados.

---

## 7. Limitações e itens abertos

- A cadeia **não está publicada** (branch local `wt/lol-78-runtime-chain`, commit
  `dc3f7df`). Enquanto não houver push/merge (fora do escopo deste card), o entrypoint
  depende do worktree `/home/alexandre/hermes-agent-runtime/.worktrees/t_0f04a754`;
  publicá-la (github-profile) permitiria fixar por instalação/commit estável.
- O caminho do PM-venv inclui um id de geração (`496d…`) que pode mudar em `hermes update`;
  por isso a resolução via `facts.json` está documentada.
- Drift da cópia PM-workspace vs checkout vivo em `hermes_cli/kanban_db.py` (ver §1) —
  reportado, não corrigido (fora do escopo).
- A implementação concreta do poller foi entregue pelo card LOL-80 / `t_153f63a4`
  (correção 3): `run_final_smoke.py` (Linear real + `LinearIssuePoller`/`Selector` +
  `RuntimeOrchestrator.run_once()` + `kanban_dispatch_adapter`), executada fora da cerca
  via `systemd-run --user` — ver `../evidence/t_153f63a4.md`.

---

## 9. Execução fora do shell cercado (validado na correção 3)

O poller muta o board, então roda como unit transiente do systemd do usuário (o local
sancionado de §3), com ambiente limpo (o marker `HERMES_DELEGATED_CHILD_CONTEXT` fica
ausente e o wrapper aceita o modo mutante):

    systemd-run --user --collect --property=Type=oneshot \
      /home/alexandre/.hermes/runtime/poller/run_poller.sh \
      /home/alexandre/.hermes/runtime/poller/run_final_smoke.py

A limpeza do board (arquivar/remover cards fixture) usa o mesmo caminho —
`archive_board_task.py`. Detalhes, evidências e baseline em
`/home/alexandre/.hermes/runtime/evidence/t_153f63a4.md`.

---

## 8. Evidências

    /home/alexandre/.hermes/runtime/evidence/t_0d5d2c4c/
      probe_readonly.out        # probe PASS + guard exit 3 + board read
      interpreter_matrix.out    # matriz dos 3 candidatos + hashes live vs PM-ws
      context.txt               # host/data/kernel + hermes --print-runtime-command
    /home/alexandre/.hermes/runtime/poller/
      run_poller.sh             # entrypoint único
      probe_env.py              # probe read-only

Cadeia versionada (LOL-78): commit `dc3f7dfcc733caa167e2173ab5fbe4da1d680b78`,
worktree `/home/alexandre/hermes-agent-runtime/.worktrees/t_0f04a754`,
`sha256[:16]` por arquivo registrados em `probe_readonly.out`.

---

## 10. Unit permanente do poller (LOL-97 / t_a4d0e749) — follow-up pós-merge

Tudo abaixo é **preparação**: a unit foi instalada mas **não habilitada nem iniciada**
(`systemctl --user is-enabled/is-active` = `disabled`/`inactive`). Nenhum deploy, nenhum
secret alterado, nenhuma produção iniciada.

### 10.1 Cadeia fixada no commit mergeado

`origin/main` @ `ade12189336abf5d28d1c9b1a8ada823577bf834` (merge do PR #5). `git diff
dc3f7df..ade1218 -- src` é vazio, ou seja o código mergeado é idêntico ao aprovado. Extração
**read-only** (sem `.git`, sem alterar o repositório) em:

    /home/alexandre/.hermes/runtime/chain/ade1218/
      src/            # PYTHONPATH da cadeia
      tests/          # suíte da cadeia (27 testes) para o canário
      PIN.json        # commit + sha256 de cada arquivo (gerado por gen_pin.py)

Regenerar após uma publicação nova:

    /home/alexandre/.hermes/installs/974d540f82c0c176/environments/496d61b5a8854999ba0e681f1ec5b777/venv/bin/python \
      /home/alexandre/.hermes/runtime/poller/gen_pin.py

(para outra versão: `git -C /home/alexandre/hermes-agent-runtime archive <commit> src tests | tar -x -C <destino>`)

### 10.2 Interpreter estável (sobrevive a `hermes update`)

`run_poller.sh` resolve o interpreter em tempo de execução: `install_key =
sha256(<checkout>)[:16]` (contrato `pm/environments.py::install_key`), lê
`~/.hermes/installs/<key>/facts.json → packages.venv.environment` e usa `<venv>/bin/python`.
Fallback: o caminho pinado da LOL-79 (`installs/974d540f82c0c176/environments/496d…/venv`).
`POLLER_PYTHON` continua sendo override de teste.

### 10.3 Entrypoint e `HERMES_HOME` explícito

    run_poller.sh --canary | --probe | --gates-test | <module-or-script.py> [args...]

O wrapper **fixa** `HERMES_HOME="$HERMES_ROOT"` (default `/home/alexandre/.hermes`, o root
compartilhado que o gateway/dispatcher usam). Um `HERMES_HOME` herdado do shell chamador
(ex.: um home de perfil) é descartado, então o canário não depende de quem o executa
(CR-2 / LOL-101). Também exporta `PYTHONPATH=<checkout vivo>:<cadeia pinada>`,
`HERMES_KANBAN_BOARD=lolcoach` e `CHAIN_PIN`. O guard de shell cercado permanece: o poller
mutante recusa iniciar (`exit 3`) se `HERMES_DELEGATED_CHILD_CONTEXT` estiver setado.
`--gates-test` roda a regressão read-only do contrato de gates CR-1 (SQLite in-memory; não
abre nem escreve o board compartilhado).

### 10.4 Unit systemd (preparada, não habilitada)

    /home/alexandre/.config/systemd/user/hermes-runtime-poller.service   (Type=oneshot)
    /home/alexandre/.config/systemd/user/hermes-runtime-poller.timer     (OnUnitActiveSec=60)

Cópias de origem: `~/.hermes/runtime/poller/hermes-runtime-poller.{service,timer}`.
`ExecStart` roda o wrapper com o entrypoint de produção de um tick:
`~/.hermes/runtime/poller/poller_tick.py` (Linear Todo → selector → AgentRuntime →
Kanban dispatch → gates → finish). O service define `HERMES_HOME`, `HERMES_KANBAN_BOARD`,
`POLLER_ENV_FILE`, `PATH` mínimo, e `NoNewPrivileges=yes`/`PrivateTmp=yes`.

Instalar/atualizar (não habilita):

    install -m 644 ~/.hermes/runtime/poller/hermes-runtime-poller.service ~/.config/systemd/user/
    install -m 644 ~/.hermes/runtime/poller/hermes-runtime-poller.timer   ~/.config/systemd/user/
    systemctl --user daemon-reload

**Habilitar/iniciar é produção e exige autorização explícita** (fora do escopo desta card):

    systemctl --user enable --now hermes-runtime-poller.timer

### 10.5 Logs

`StandardOutput=journal` / `StandardError=journal`, `SyslogIdentifier=hermes-runtime-poller`:

    journalctl --user -u hermes-runtime-poller.service -f
    journalctl --user -u hermes-runtime-poller.service --since today

### 10.6 Canário read-only

`run_poller.sh --canary` (implementação `canary_readonly.py`) valida, **sem mutar nada**, 16–17
checagens: interpreter/versão do venv, `HERMES_HOME` explícito, commit+sha256 da cadeia pinada,
11/11 imports, leitura do board lolcoach + ausência de resíduo do poller, suíte 27/27 da cadeia,
presença/parse das unit files (`systemd-analyze verify`), arquivo de segredos modo 600 com as 3
chaves, e (com `POLLER_CANARY_NETWORK=1`) uma leitura Supabase read-only. Execução out-of-fence
reproduzível:

    systemd-run --user --collect --wait --pipe --property=Type=oneshot \
      --setenv=HERMES_HOME=/home/alexandre/.hermes \
      --setenv=HERMES_KANBAN_BOARD=lolcoach \
      --setenv=POLLER_ENV_FILE=/home/alexandre/.hermes/shared/.env \
      /home/alexandre/.hermes/runtime/poller/run_poller.sh --canary

`selftest_tick_import.py` valida o import do alvo real de `ExecStart` sob o ambiente da unit sem
executar um tick.

### 10.7 Rollback (reverter a unit permanente)

    systemctl --user stop hermes-runtime-poller.timer hermes-runtime-poller.service  # idempotente
    systemctl --user disable hermes-runtime-poller.timer
    rm -f ~/.config/systemd/user/hermes-runtime-poller.{service,timer}
    systemctl --user daemon-reload
    # artefatos do card (opcional):
    rm -rf ~/.hermes/runtime/chain/ade1218
    rm -rf ~/.hermes/runtime/evidence/t_a4d0e749

Nada foi instalado por `pip`, nenhum serviço ficou habilitado, o repositório
`hermes-agent-runtime` não foi alterado (só `git archive`), e o checkout vivo / PM-venv /
board / Supabase / Linear ficaram intactos.

### 10.8 Evidências

    /home/alexandre/.hermes/runtime/evidence/t_a4d0e749.md
    /home/alexandre/.hermes/runtime/evidence/t_a4d0e749/
      canary_worker.out         # canary 16/16 no shell do worker
      canary_systemd_run.out    # canary 17/17 out-of-fence (inclui Supabase read-only)
      execstart_selftest.out    # import do alvo de ExecStart sob o ambiente da unit
      probe_regression.out      # --probe da LOL-79 continua PASS
      state.txt                 # host/git/unit/hashes

### 10.9 Guard de dedup/policy/bound do tick (LOL-102 / t_50a168bb)

**Motivo.** A ativação da LOL-102 (`t_c586fe2d`) mostrou que um tick de produção
integral despacharia o backlog Linear `Todo` inteiro de uma vez — duplicando cards
já existentes no board e roteando trabalho sensível — e que a unit (`TimeoutStartSec=3600`)
seria truncada no meio. Correção implementada no entrypoint de produção
(`poller_tick.py` + `poller_dispatch_guard.py`), sem ligar a unit.

**O que o tick passou a fazer** (`guarded_read_issues`, antes de qualquer claim):
1. **dedup por `linear_issue_id`** — issue que já tem card não-arquivado no board é
   ignorada (o id do card existente é logado); arquivar é o jeito explícito de liberar
   novo despacho. Casa título `[runtime] <ISSUE>` ou corpo com o identificador
   (com fronteira de palavra: `LOL-9` não casa `LOL-90`/`LOL-900`).
2. **policy** — nunca despacha `risk:high`/`risk:critical`, `execution:human`/
   `execution:blocked`, `env:production`, nem issue com gate ausente/ambíguo/inválido
   (`agent:*`/`risk:*`/`execution:*`/`env:*`).
3. **bound por tick** — no máximo `POLLER_MAX_DISPATCH_PER_TICK` (default **1**); o
   restante é ignorado com motivo `bound` e pego no próximo tick.

`start()` reforça: re-checa o board por `linear_issue_id` antes de `create_task` e usa
`idempotency_key="runtime:linear:<ISSUE>"` (o `create_task` devolve o card existente em
vez de duplicar). Contexto com gatilho humano é bloqueado no runtime (`run.blocked`).

**Configurar o bound** (ex.: 3 por tick) — adicione à unit e recarregue (sem habilitar):

    # ~/.config/systemd/user/hermes-runtime-poller.service (Environment=)
    Environment=POLLER_MAX_DISPATCH_PER_TICK=3

Default (sem env) = 1. Valor inválido → fallback para 1 com warning no journal.

**Testes read-only** (`--guard-test`, in-fence 36/36 + 2 skips; out-of-fence 41/41):

    run_poller.sh --guard-test
    # contrato create_task exige write: rode out-of-fence
    systemd-run --user --collect --wait --pipe --property=Type=oneshot \
      --setenv=HERMES_HOME=/home/alexandre/.hermes --setenv=HERMES_KANBAN_BOARD=lolcoach \
      /home/alexandre/.hermes/runtime/poller/run_poller.sh --guard-test

**Rollback** (guard é aditivo; reverter ao tick anterior):
remova `poller_dispatch_guard.py`/`test_poller_dispatch_guard.py`, o case `--guard-test`
do `run_poller.sh` e reverta `poller_tick.py` para `LinearIssuePoller(read_issues, ...)`
com `idempotency_key=f"runtime:{context.run_id}"`. A unit permanece disabled/inactive.

### 10.10 Correção BUG-01 — import do `start()` e cobertura (LOL-103 / t_3374c665)

**Motivo.** O QA (`t_494664af`) reprovou a entrega da §10.9: `poller_tick.start()`
fazia `from hermes_agent_runtime import ExecutionBlocked`, mas a cadeia pinada
(`ade1218`) não re-exporta essa classe no `__init__.py` (ela vive em
`hermes_agent_runtime/runtime.py`). Resultado: `ImportError` em **toda** chamada de
`start()` — o re-check de board, o `idempotency_key` e o bloqueio de contexto humano
nunca executavam; a evidência passou porque nenhum teste chamava `start()`.

**Correção** (1 linha, cadeia intocada):
`from hermes_agent_runtime.runtime import ExecutionBlocked`.

**Cobertura nova** (`--guard-test`, seção “start(): execute the real defense-in-depth
path”): executa o `start()` real com connection factory monkeypatchada para um SQLite
em memória — nenhum acesso ao board compartilhado:
- card existente → skip, retorna o id existente, `create_task` não é chamado;
- `risk:high`/`critical`, `execution:human`, `env:production` → `ExecutionBlocked`
  antes de qualquer write;
- issue elegível → `create_task` real com `[runtime] <ISSUE>`,
  `idempotency_key=runtime:linear:<ISSUE>`, `created_by=runtime-poller` e 1 linha;
- chamada repetida vê o card criado → skip sem nova linha (dedup).

Contagens: in-fence **36/36 + 2 skips**, out-of-fence **41/41**. Regressões
`--gates-test` 6/6 e `--canary` 16/16 preservadas. Unit **disabled/inactive**.

**Rollback** da correção: repor a linha antiga
`from hermes_agent_runtime import ExecutionBlocked` (volta o `ImportError`) e remover a
seção de `start()` do `test_poller_dispatch_guard.py`. Não afeta a unit nem a cadeia.

