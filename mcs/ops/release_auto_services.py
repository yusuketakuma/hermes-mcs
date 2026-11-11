"""v3 service drain deps面: stop_new_intake/managed_services/request_stop/
await_retirement/retirement_complete/restart_same_versionの orchestration。

spec 02 §6: 新規受付停止→各管理processへ安全停止要求→全員退役を
process instance/native birth/boot witness/generation/Q共有holderで証明
(PID単独・pgrep単独・古いheartbeat単独は不可)→失敗時は同一版だけ再起動。
drain待機はlock保持中に行わない(ownerがlock外で呼ぶ)。

service記述子・個別stop要求・退役probe・同版再起動の実体はfactory注入 —
hermes launchd agent/standalone supervisor/LINE WORKS/cron child等の
per-service機構は配備構成の責務。本moduleは順序・deadline・証拠完全性・
fail-closed判定のorchestrationを担う:

- servicesはlistまたはcallable。callableはmanaged_services()呼出毎に
  fresh評価する(prepare時点の実行中processだけがdrain対象になる)
- stoppers/probersは{id: fn}またはcallable(service)のどちらでもよい。
  callable形は動的idを扱える(launchd drainのようにlabel集合が
  実行時に決まる場合)
- request_stopはmanaged_services全件へ順に要求(個別失敗は例外=中断)
- await_retirementはdeadlineまで全proberをpollし、退役証拠を収集する。
  deadline超過・probe失敗・未退役は例外にせずevidenceへ記録して返す
  (ownerがretirement_completeで最終判定する設計と一致)
- retirement_completeは要求service全idの新鮮な退役証拠を要求する
- restart_same_versionは注入restarterへ委譲(冪等性はrestarter側契約)
"""
import time


class ServiceError(RuntimeError):
    """service drain失敗。codeは固定token。"""

    def __init__(self, code='SERVICE_FAILED'):
        super().__init__(code)
        self.code = code


def _validate_services(services):
    if (not isinstance(services, (list, tuple)) or not services
            or any(not isinstance(s, dict) or type(s.get('id')) is not str
                   or not s['id'] for s in services)):
        raise ServiceError('SERVICE_INVENTORY')
    ids = [s['id'] for s in services]
    if len(set(ids)) != len(ids):
        raise ServiceError('SERVICE_INVENTORY')
    return [dict(s) for s in services]


class ServiceDrain:
    """deps契約のservice cluster面。

    services: drain対象のservice記述子list、またはそれを返すcallable
        (呼出毎にfresh評価。各dictは'id'を持つ)。
    stoppers: {service_id: callable(service)->None}または
        callable(service)->None — 安全停止要求。
    probers: {service_id: callable(service)->dict|None}または
        callable(service)->dict|None — None=未退役、
        dict=退役証拠(process instance/birth/boot/generation照合済み)。
    restarter: callable()->None — 同一版再起動(冪等)。
    intake_stop: callable()->None — 新規受付停止(marker書込み等)。
    now_ms/mono/sleep: 時刻・待機の注入(試験で決定論化)。
    poll_interval_s: await中のpoll間隔(既定0.5s)。
    """

    def __init__(self, *, services, stoppers, probers, restarter,
                 intake_stop, now_ms, mono=time.monotonic, sleep=time.sleep,
                 poll_interval_s=0.5):
        self._services_param = services
        self._stoppers = stoppers
        self._probers = probers
        self._restarter = restarter
        self._intake_stop = intake_stop
        self._now_ms = now_ms
        self._mono = mono
        self._sleep = sleep
        self._poll = poll_interval_s
        self._active = None
        self._last_evidence = None
        if not callable(services):
            resolved = _validate_services(services)
            ids = {s['id'] for s in resolved}
            for table in (stoppers, probers):
                if isinstance(table, dict) and set(table) != ids:
                    raise ServiceError('SERVICE_INVENTORY')
                if not callable(table) and not isinstance(table, dict):
                    raise ServiceError('SERVICE_INVENTORY')
            self._active = resolved

    @property
    def last_evidence(self):
        """直近のawait_retirement戻り値(witness provider結線用)。"""
        return self._last_evidence

    def _resolve_services(self):
        """managed_services未呼出ならproviderを今評価する。"""
        if self._active is None:
            provider = self._services_param
            try:
                resolved = provider() if callable(provider) else provider
            except ServiceError:
                raise
            except Exception as e:
                raise ServiceError('SERVICE_INVENTORY') from e
            self._active = _validate_services(resolved)
        return self._active

    def _fn(self, table, service):
        sid = service.get('id') if isinstance(service, dict) else None
        if callable(table):
            return table
        fn = table.get(sid) if isinstance(table, dict) else None
        if fn is None:
            raise ServiceError('SERVICE_UNKNOWN')
        return fn

    # --- deps契約 ------------------------------------------------------

    def stop_new_intake(self):
        try:
            self._intake_stop()
        except Exception as e:
            raise ServiceError('INTAKE_STOP') from e

    def managed_services(self):
        """呼出毎にproviderをfresh評価する — prepare時点の実行中process。"""
        provider = self._services_param
        try:
            resolved = provider() if callable(provider) else provider
        except ServiceError:
            raise
        except Exception as e:
            raise ServiceError('SERVICE_INVENTORY') from e
        self._active = _validate_services(resolved)
        return [dict(s) for s in self._active]

    def request_stop(self, service):
        try:
            stop = self._fn(self._stoppers, service)
        except ServiceError:
            raise
        except Exception as e:
            raise ServiceError('SERVICE_STOP') from e
        try:
            stop(service)
        except Exception as e:
            raise ServiceError('SERVICE_STOP') from e

    def await_retirement(self, deadline_epoch_ms):
        """deadlineまで全proberをpollし退役証拠を返す。

        戻り値: {'observed_at_ms','deadline_epoch_ms','services':
                 {id: {'retired': bool, 'proof': dict|None,
                       'error': str|None}}}
        deadlineはmono時計で評価し、超過時は未証明をretired=Falseのまま返す。
        """
        if type(deadline_epoch_ms) is not int:
            raise ServiceError('SERVICE_DEADLINE')
        services = self._resolve_services()
        deadline_mono = self._mono() + max(
            0.0, (deadline_epoch_ms - self._now_ms()) / 1000.0)
        results = {s['id']: {'retired': False, 'proof': None, 'error': None}
                   for s in services}
        while True:
            pending = False
            for service in services:
                sid = service['id']
                row = results[sid]
                if row['retired']:
                    continue
                try:
                    probe = self._fn(self._probers, service)
                    proof = probe(service)
                except Exception as e:
                    row['error'] = type(e).__name__
                    pending = True
                    continue
                if isinstance(proof, dict) and proof:
                    row['retired'] = True
                    row['proof'] = proof
                else:
                    pending = True
            if not pending or self._mono() >= deadline_mono:
                break
            self._sleep(self._poll)
        self._last_evidence = {'observed_at_ms': self._now_ms(),
                               'deadline_epoch_ms': deadline_epoch_ms,
                               'services': results}
        return self._last_evidence

    def retirement_complete(self, evidence, services):
        """evidenceが要求service全idの退役証拠を持つか検査する。"""
        if not isinstance(evidence, dict) or not isinstance(services, (list, tuple)):
            return False
        rows = evidence.get('services')
        if not isinstance(rows, dict):
            return False
        for service in services:
            sid = service.get('id') if isinstance(service, dict) else None
            row = rows.get(sid)
            if (not isinstance(row, dict) or row.get('retired') is not True
                    or not isinstance(row.get('proof'), dict)
                    or not row['proof']):
                return False
        return True

    def restart_same_version(self):
        """中断prepareの安全復帰。注入restarterへ委譲(冪等はrestarter契約)。"""
        try:
            self._restarter()
        except Exception as e:
            raise ServiceError('SERVICE_RESTART') from e
