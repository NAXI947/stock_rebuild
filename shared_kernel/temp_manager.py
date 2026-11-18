"""Linux task workspaces with process leases and a shared write budget."""
from contextlib import contextmanager
import fcntl
from io import BytesIO
from itertools import islice
import logging
import os
from pathlib import Path
import re
import shutil
import stat
from uuid import uuid4

LOG = logging.getLogger(__name__)
ID = re.compile(r"task-[a-f0-9]{32}\Z")
LEAF = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
CHUNK = 64 * 1024


class BudgetError(RuntimeError):
    pass


def lock_file(path, *, create=False):
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | (os.O_CREAT if create else 0), 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_nlink != 1:
        os.close(fd)
        raise ValueError("lock is not a private regular file")
    return os.fdopen(fd, "rb+")


class TempManager:
    def __init__(self, root, *, total=None, per_task=None, entries=None,
                 persistent=None, min_free=None, service=None):
        total = int(os.environ.get("APP_TMP_BUDGET_BYTES", 2**31)) if total is None else total
        per_task = int(os.environ.get("APP_TMP_TASK_BUDGET_BYTES", 2**28)) if per_task is None else per_task
        entries = int(os.environ.get("APP_TMP_ENTRY_LIMIT", 20000)) if entries is None else entries
        min_free = int(os.environ.get("STORAGE_MIN_FREE_BYTES", 5*2**30)) if min_free is None else min_free
        service = service or os.environ.get("APP_SERVICE_NAME")
        if service not in (None, "api", "worker", "web"):
            raise ValueError("unknown temporary service")
        root = Path(root)
        if not root.is_absolute() or root.resolve()!=root or not root.is_dir():
            raise ValueError("APP_TMP_ROOT must be an existing real absolute directory")
        if root.stat().st_mode & 0o077 or root.stat().st_uid != os.geteuid():
            raise ValueError("APP_TMP_ROOT must be private and owned by the service")
        if not 0 < per_task <= total or entries < 2 or min_free < 0:
            raise ValueError("invalid temporary budgets")
        self.root, self.total, self.per_task, self.entries = root,total,per_task,entries
        self.persistent = Path(persistent) if persistent else root
        if self.persistent == root or self.persistent.is_relative_to(root):
            if persistent:
                raise ValueError("persistent results cannot live in APP_TMP_ROOT")
        self.min_free = min_free
        self.device = root.stat().st_dev
        parent = root / service if service else root
        if service:
            parent.mkdir(mode=0o700, exist_ok=True)
            self._safe_dir(parent)
        self.jobs = parent / ("jobs" if service else "stage1")
        self.jobs.mkdir(mode=0o700, exist_ok=True)
        self.device = root.stat().st_dev
        self._safe_dir(self.jobs)

    def _safe_dir(self, p):
        if p.resolve() != p or not p.is_relative_to(self.root):
            raise ValueError("workspace escapes managed root")
        s = p.lstat()
        if (not stat.S_ISDIR(s.st_mode) or s.st_dev!=self.device or (p != self.root and os.path.ismount(p))
                or s.st_uid!=os.geteuid() or s.st_mode & 0o077):
            raise ValueError(f"unsafe workspace directory: {p.name}")

    @contextmanager
    def _locked(self):
        self._safe_dir(self.jobs)
        with lock_file(self.root/".guard",create=True) as guard:
            fcntl.flock(guard,fcntl.LOCK_EX)
            yield

    def _usage(self, directory, *, limit=None):
        self._safe_dir(directory)
        total,count = 0,0
        for item in directory.iterdir():
            if limit is not None and count >= limit:
                raise BudgetError("ROOT_TEMP_BUDGET")
            st=item.lstat()
            if stat.S_ISDIR(st.st_mode):
                used,num=self._usage(item,limit=None if limit is None else limit-count-1)
                total+=used
                count+=num+1
            else:
                # Unexpected contents are counted if safe; never follow links.
                if not stat.S_ISREG(st.st_mode) or st.st_nlink!=1 or st.st_dev!=self.device:
                    raise ValueError(f"unmanaged or unsafe workspace entry: {item.name}")
                total+=st.st_size
                count+=1
        return total,count

    def _check(self, path=None, delta=0, entries=0):
        if (shutil.disk_usage(self.persistent).free < self.min_free
                or shutil.disk_usage(self.root).free < max(delta,1)):
            raise BudgetError("LOW_DISK: cannot stage new data")
        total,count = self._usage(self.root, limit=self.entries)
        if total >= self.total * 4 // 5:
            LOG.warning("Stage 1 temporary budget at or above 80%")
            self._collect_locked()
            total,count = self._usage(self.root, limit=self.entries)
        if total+delta>self.total or count+entries>self.entries:
            raise BudgetError("ROOT_TEMP_BUDGET")
        if path and self._usage(path)[0]+delta>self.per_task:
            raise BudgetError("TASK_TEMP_BUDGET")

    @contextmanager
    def task(self):
        with self._locked():
            self._check(entries=2)
            path=self.jobs/("task-"+uuid4().hex)
            path.mkdir(mode=0o700)
            lease=lock_file(path/".lease",create=True)
            fcntl.flock(lease,fcntl.LOCK_EX)
        try:
            yield Workspace(self,path)
        finally:
            with self._locked():
                try:
                    self._discard(path)
                except (OSError,ValueError):
                    LOG.exception("Stage 1 workspace cleanup failed: %s",path.name)
                    raise
                finally:
                    lease.close()

    def _discard(self,path,*,limit=None):
        self._safe_dir(path)
        # Keep files while a local response/SDK still owns a descriptor. A lost
        # lease alone must not turn an open file into invisible disk usage.
        for descriptor in Path("/proc/self/fd").iterdir():
            try:
                target = Path(os.readlink(descriptor))
            except (FileNotFoundError, OSError):
                continue
            if target.is_absolute() and target.is_relative_to(path) and target.name != ".lease":
                raise ValueError("workspace still has an open file handle")
        entries=path.iterdir()
        children=list(entries if limit is None else islice(entries,limit+1))
        if limit is not None and len(children)>limit:
            raise BudgetError("TASK_TEMP_ENTRY_LIMIT")
        for p in children:
            st=p.lstat()
            if not (p.name==".lease" or LEAF.fullmatch(p.name)):
                raise ValueError("unrecognized workspace entry")
            if not stat.S_ISREG(st.st_mode) or st.st_nlink!=1 or st.st_dev!=self.device:
                raise ValueError("unsafe workspace content")
        for p in children:
            if p.name!=".lease":
                p.unlink()
        (path/".lease").unlink()
        path.rmdir()

    def _collect_jobs(self, jobs, limit=None):
        removed, failed = [], []
        self._safe_dir(jobs)
        for path in islice(jobs.iterdir(),self.entries):
            if limit is not None and len(removed)+len(failed)>=limit:
                break
            if not ID.fullmatch(path.name):
                continue
            try:
                self._safe_dir(path)
                with lock_file(path/".lease") as lease:
                    try:
                        fcntl.flock(lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue
                    self._discard(path,limit=self.entries)
                removed.append(path.name)
            except (OSError,ValueError,BudgetError) as exc:
                failed.append({"task":path.name,"reason":str(exc)})
                LOG.warning("Stage 1 recovery skipped %s: %s",path.name,exc)
        return {"removed":removed,"failed":failed}

    def _collect_locked(self, limit=None):
        report = {"removed": [], "failed": []}
        candidates = [self.root/"stage1"]
        for service in ("api", "worker", "web"):
            candidates.extend((self.root/service/"jobs", self.root/service/"stage1"))
        for jobs in candidates:
            if not jobs.exists():
                continue
            remaining = None if limit is None else limit - len(report["removed"]) - len(report["failed"])
            if remaining is not None and remaining <= 0:
                break
            found = self._collect_jobs(jobs, remaining)
            for key in report:
                report[key].extend(found[key])
        return report

    def collect(self, *, limit=None):
        if limit is not None and limit < 1:
            raise ValueError("cleanup limit must be positive")
        with self._locked():
            return self._collect_locked(limit)

    def usage(self):
        """Measure registered workspaces under the guard, with a bounded entry scan."""
        with self._locked():
            return self._usage(self.root,limit=self.entries)

    def breakdown(self):
        with self._locked():
            return {role: dict(zip(("bytes", "entries"), self._usage(self.root/role, limit=self.entries)))
                    for role in ("api", "worker", "web") if (self.root/role).exists()}


class Workspace:
    def __init__(self,manager,path):
        self.manager,self.path=manager,path

    def stage(self,name,source):
        if not LEAF.fullmatch(name):
            raise ValueError("temporary filename must be a single safe name")
        target=self.path/name
        with self.manager._locked():
            self.manager._check(self.path,entries=1)
            fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,"wb",buffering=0) as f:
            while data:=source.read(CHUNK):
                with self.manager._locked():
                    self.manager._check(self.path,len(data))
                    if f.write(data)!=len(data):
                        raise OSError("short temporary write")
        return target


def store_file(manager, directory, name, content):
    destination=directory/name
    if destination.is_symlink():
        raise ValueError("artifact path is a symbolic link")
    if destination.exists():
        if destination.read_bytes()!=content:
            raise ValueError("artifact version collides with different contents")
        return str(destination)
    with manager.task() as work:
        staged=work.stage(name,BytesIO(content))
        with staged.open("rb") as stream:
            os.fsync(stream.fileno())
        os.replace(staged,destination)
        fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return str(destination)
