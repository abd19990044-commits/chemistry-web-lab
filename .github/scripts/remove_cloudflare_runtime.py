from pathlib import Path

p=Path('orca_orchestrator/service.py')
text=p.read_text(encoding='utf-8')
text=text.replace('from .cloudflare_controller import CloudflareController\n','')
text=text.replace('from .errors import (ConcurrencyError, NotFoundError, OrchestratorError,\n                     ValidationError)\n','from .errors import (ConcurrencyError, IntegrityError, NotFoundError, OrchestratorError,\n                     ValidationError)\n')
old='''        self.reconciler = Reconciler(self.store)\n        self.cf_controller = CloudflareController(store=self.store)\n        self.result_store = ResultArtifactStore()\n        self.watchdog = Watchdog(\n            self.store, self.reconciler, BROKER,\n            workflow_driver=self._reconcile_and_drive_workflows,\n        )\n'''
new='''        self.reconciler = Reconciler(self.store)\n        self.result_store = ResultArtifactStore()\n        self.watchdog = Watchdog(self.store, self.reconciler, BROKER)\n'''
assert old in text
text=text.replace(old,new,1)
# Remove the legacy Cloudflare workflow execution section and replace with a
# clearly non-durable compatibility surface. This prevents login/listing from
# touching Cloudflare at all.
start=text.index('    # -- workflow execution')
end=text.index('    # -- results', start)
replacement='''    # -- workflows ---------------------------------------------------------\n    # Cloudflare-backed workflow orchestration has been retired. Restart chains\n    # are now Kaggle-native and self-continuing. The legacy workflow API stays\n    # readable so older frontends do not crash, but it no longer creates any\n    # external control-plane dependency.\n    def submit_workflow(self, creds: KaggleCredentials, title: str, steps: list[dict[str, Any]],\n                        *, aux_files: dict[str, bytes] | None = None,\n                        dataset_sources: list[str] | None = None,\n                        orca_link: str | None = None) -> dict[str, Any]:\n        raise ValidationError(\n            "Cloudflare-backed multi-step workflows were removed. Submit the ORCA job "\n            "normally; restart/continuation state is now carried entirely by Kaggle."\n        )\n\n    def get_workflow(self, creds: KaggleCredentials, workflow_id: str) -> dict[str, Any] | None:\n        return None\n\n    def list_workflows(self, creds: KaggleCredentials) -> list[dict[str, Any]]:\n        return []\n\n\n'''
text=text[:start]+replacement+text[end:]
# Remove Cloudflare result-state syncs and delete calls.
import re
text=re.sub(r'\n\s*try:\n\s*self\.cf_controller\.sync_job_state\(job\)\n\s*except Exception(?: as exc)?:\n(?:\s*log\.warning\([^\n]+\)\n|\s*pass\n)', '\n', text)
text=re.sub(r'\n\s*try:\n\s*self\.cf_controller\.client\.delete_job\(job_id, auth_creds\.username\)\n\s*except Exception:\n\s*pass\n', '\n', text)
p.write_text(text,encoding='utf-8')

# API workflow endpoint: GET remains compatible, POST explains the new model.
p=Path('orca_orchestrator/api.py')
text=p.read_text(encoding='utf-8')
# service.submit_workflow already raises a clear ValidationError; no further change needed.
p.write_text(text,encoding='utf-8')
print('Cloudflare runtime dependency removed')