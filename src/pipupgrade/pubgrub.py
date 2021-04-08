import os.path as osp
import gzip
from   datetime import datetime as dt
import json
import pkg_resources

from pipupgrade._compat import iteritems, iterkeys
from pipupgrade.log     import get_logger
from pipupgrade.config  import PATH, Settings
from pipupgrade         import request as req
from pipupgrade._pip    import parse_requirements
from pipupgrade.util.system  import make_temp_dir, write
from pipupgrade.model.package import Package

from semver import Version, VersionRange, parse_constraint

from mixology.constraint     import Constraint
from mixology.package_source import PackageSource as BasePackageSource
from mixology.range          import Range
from mixology.union          import Union

logger   = get_logger()
settings = Settings()

def populate_db():
    dt_now = dt.now()

    logger.info("Populating DB...")

    path_gzip = osp.join(PATH["CACHE"], "dependencies.json.gz")
    path_uzip = osp.join(PATH["CACHE"], "dependencies.json")

    refresh   = False

    if not osp.exists(path_gzip):
        refresh = True
    else:
        time_modified = dt.fromtimestamp( osp.getmtime(path_gzip) )
        cache_seconds = settings.get("cache_timeout")
        delta_seconds = (time_modified - dt_now).total_seconds()

        if delta_seconds > cache_seconds:
            refresh = True

    if refresh:
        logger.info("Fetching Dependency Graph...")

        response = req.get("https://github.com/achillesrasquinha/pipupgrade/blob/master/data/dependencies.json.gz?raw=true",
            stream = True)

        if response.ok:
            with open(path_gzip, "wb") as f:
                for content in response.iter_content(chunk_size = 1024):
                    f.write(content)
        else:
            response.raise_for_status()

        with gzip.open(path_gzip, "rb") as rf:
            with open(path_uzip, "wb") as wf:
                content = rf.read()
                wf.write(content)

_DEPENDENCIES = {}

def _parse_dependencies(deps):
    # reqs = []

    # with make_temp_dir() as dir_path:
    #     path_file = osp.join(dir_path, "requirements.txt")
    #     write(path_file, "\n".join(deps))

    #     reqs = [req for req in parse_requirements(path_file, session = "hack")]

    # return reqs
    return [ pkg_resources.Requirement.parse(dep) for dep in deps ]

def get_meta(package, version):
    global _DEPENDENCIES
    
    if not _DEPENDENCIES:
        path_dependencies = osp.join(PATH["CACHE"], "dependencies.json")

        with open(path_dependencies) as f:
            _DEPENDENCIES = json.load(f)

    data = _DEPENDENCIES.get(package.name, {})

    dependencies = _parse_dependencies(data.get(version) or [])
    
    return {
        "releases": list(iterkeys(data)),
        "dependencies": dependencies
    }

class Dependency:
    def __init__(self, package, constraint = None):
        self.name               = package.name
        self.constraint         = parse_constraint(constraint or "*")
        self.pretty_constraint  = constraint

    def __str__(self):
        return self.pretty_constraint

class PackageSource(BasePackageSource):
    def __init__(self, *args, **kwargs):
        self._root_version      = Version.parse("0.0.0")
        self._root_dependencies = [ ]
        self._packages          = { }

        self.super = super(PackageSource, self)
        self.super.__init__(*args, **kwargs)

    @property
    def root_version(self):
        return self._root_version

    def add(self, name, extras, version, deps = None):
        version = Version.parse(version)
        if name not in self._packages:
            self._packages[name] = { extras: {} }
        if extras not in self._packages[name]:
            self._packages[name][extras] = {}

        if version in self._packages[name][extras] and not (
            deps is None or self._packages[name][extras][version] is None
        ):
            raise ValueError("{} ({}) already exists".format(name, version))

        if deps is None:
            self._packages[name][extras][version] = None
        else:
            dependencies = [ ]
            # for dep_name, spec in iteritems(deps):
            #     dependencies.append(Dependency(dep_name, spec))
            for dep in deps:
                dependencies.append(Dependency(dep))

            self._packages[name][extras][version] = dependencies

    def root_dep(self, package, constraint):
        dependency   = Dependency(package, constraint)
        self._root_dependencies.append(dependency)

        metadata     = get_meta(package, constraint)

        for release in metadata["releases"]:
            self.add(package.name, package.extras, release)

        deps = []
        for dependency in metadata["dependencies"]:
            deps.append(Package(dependency.name))

        self.add(package.name, package.extras, constraint, deps = deps)

    def _versions_for(self, package, constraint = None):
        if package not in self._packages:
            return [ ]

        versions = [ ]
        for version in iterkeys(self._packages[package]):
            if not constraint or constraint.allows_any(
                Range(version, version, True, True)
            ):
                versions.append(version)

        return sorted(versions, reverse = True)

    def dependencies_for(self, package, version):
        if package == self.root:
            return self._root_dependencies
        return self._packages[package][version]

    def convert_dependency(self, dependency):
        if isinstance(dependency.constraint, VersionRange):
            constraint = Range(
                dependency.constraint.min,
                dependency.constraint.max,
                dependency.constraint.include_min,
                dependency.constraint.include_max,
                dependency.pretty_constraint,
            )
        else:
            ranges = [
                Range(
                    _range.min,
                    _range.max,
                    _range.include_min,
                    _range.include_max,
                    str(_range),
                )
                for _range in dependency.constraint.ranges
            ]
            constraint = Union.of(ranges)

        return Constraint(dependency.name, constraint)