# imports - compatibility imports
from pipupgrade._compat import iteritems

# imports - standard imports
import sys, os, os.path as osp
import re
import glob

# imports - module imports
from pipupgrade.model         import Project
from pipupgrade.commands.util import cli_format
from pipupgrade.table      	  import Table
from pipupgrade.util.string   import strip, pluralize
from pipupgrade.util.system   import read, write, popen
from pipupgrade.util.environ  import getenvvar
from pipupgrade.util.datetime import get_timestamp_str
from pipupgrade 		      import _pip, request as req, cli, semver
from pipupgrade.__attr__      import __name__

_SEMVER_COLOR_MAP = dict(
	major = cli.RED,
	minor = cli.YELLOW,
	patch = cli.GREEN
)

def _cli_format_semver(version, type_):
	def _format(x):
		return cli_format(x, cli.YELLOW)
	
	try:
		semver.parse(version)
		
		if type_ == "major":
			version    = _format(version)
		if type_ == "minor":
			index      = version.find(".", 1) + 1
			head, tail = version[:index], version[index:]
			version    = "".join([head, _format(tail)])
		if type_ == "patch":
			index      = version.find(".", 2) + 1
			head, tail = version[:index], version[index:]
			version    = "".join([head, _format(tail)])
	except ValueError:
		pass

	return version

def _get_pypi_info(name, raise_err = True):
	url  = "https://pypi.org/pypi/{}/json".format(name)
	res  = req.get(url)

	info = None

	if res.ok:
		data = res.json()
		info = data["info"]
	else:
		if raise_err:
			res.raise_for_status()

	return info

class PackageInfo:
	def __init__(self, package):
		if   isinstance(package, (_pip.Distribution, _pip.DistInfoDistribution, _pip.EggInfoDistribution)):
			self.name            = package.project_name
			self.current_version = package.version
		elif isinstance(package, _pip.InstallRequirement):
			self.name            = package.name
			self.current_version = package.installed_version

		_pypi_info = _get_pypi_info(self.name, raise_err = False) or { }

		self.latest_version = _pypi_info.get("version")
		self.home_page      = _pypi_info.get("home_page")

def _update_requirements(path, package):
	path 	= osp.realpath(path)
	
	content = read(path)
		
	try:
		pattern = r"{package}(=={version})*".format(
			package = re.escape(package.name),
			version = re.escape(package.current_version)
		)
		lines   = content.splitlines()
		nlines  = len(lines)
		
		with open(path, "w") as f:
			for i, line in enumerate(lines):
				if re.search(pattern, line, flags = re.IGNORECASE):
					line = line.replace(
						"==%s" % package.current_version,
						"==%s" % package.latest_version
					)
					
				f.write(line)

				if i < nlines - 1:
					f.write("\n")
	except Exception:
		# In case we fucked up!
		write(path, content, force = True)

def _get_included_requirements(filename):
	path         = osp.realpath(filename)
	basepath     = osp.dirname(path)
	requirements = [ ]

	with open(path) as f:
		content = f.readlines()

		for line in content:
			line = strip(line)

			if line.startswith("-r "):
				filename = line.split("-r ")[1]
				realpath = osp.join(basepath, filename)
				requirements.append(realpath)

				requirements += _get_included_requirements(realpath)

	return requirements

@cli.command
def command(
	requirements 		= [ ],
	project      		= None,
	pull_request 		= False,
	git_username 		= None,
	git_email    		= None,
	github_access_token = None,
	latest				= False,
	self 		 		= False,
	user		 		= False,
	check		 		= False,
	interactive  		= False,
	yes			 		= False,
	no_color 	 		= True,
	verbose		 		= False
):
	cli.echo(cli_format("Checking...", cli.YELLOW))
	
	registry = dict()

	if self:
		package = __name__

		_pip.install(package, user = user, quiet = not verbose, no_cache_dir = True, upgrade = True)
		cli.echo("%s upto date." % cli_format(package, cli.CYAN))
	else:
		if project:
			requirements = requirements or [ ]

			for i, p in enumerate(project):
				project[i]    = Project(osp.abspath(p))
				requirements += project[i].requirements

		if requirements:
			for requirement in requirements:
				path = osp.realpath(requirement)

				if not osp.exists(path):
					cli.echo(cli_format("{} not found.".format(path), cli.RED))
					sys.exit(os.EX_NOINPUT)
				else:
					requirements += _get_included_requirements(requirement)

			for requirement in requirements:
				path = osp.realpath(requirement)

				if not osp.exists(path):
					cli.echo(cli_format("{} not found.".format(path), cli.RED))
					sys.exit(os.EX_NOINPUT)
				else:
					registry[path] = _pip.parse_requirements(requirement, session = "hack")
		else:
			registry["__INSTALLED__"] = _pip.get_installed_distributions()

		for source, packages in iteritems(registry):
			table = Table(header = ["Name", "Current Version", "Latest Version", "Home Page"])
			dinfo = [ ] # Information DataFrame

			for package in packages:
				package = PackageInfo(package)
				package.source = source

				if package.latest_version and package.current_version != package.latest_version:
					diff_type = None

					try:
						diff_type = semver.difference(package.current_version, package.latest_version)
					except (TypeError, ValueError):
						pass

					table.insert([
						cli_format(package.name, _SEMVER_COLOR_MAP.get(diff_type, cli.CLEAR)),
						package.current_version or "na",
						_cli_format_semver(package.latest_version, diff_type),
						cli_format(package.home_page, cli.CYAN)
					])

					package.diff_type = diff_type

					dinfo.append(package)

				if package.source != "__INSTALLED__":
					_update_requirements(package.source, package)

			stitle = "Installed Distributions" if source == "__INSTALLED__" else source

			if not table.empty:
				string = table.render()
			
				cli.echo("\nSource: %s\n" % stitle)
				
				if not interactive:
					cli.echo(string)
					cli.echo()

				if not check:
					packages  = [p for p in dinfo if p.diff_type != "major" or latest]
					npackages = len(packages)

					spackages = pluralize("package", npackages) # Packages "string"
					query     = "Do you wish to update %s %s?" % (npackages, spackages)

					if npackages and (yes or interactive or cli.confirm(query, quit_ = True)):
						for i, package in enumerate(packages):
							update = True
							
							query  = "%s (%s > %s)" % (
								cli_format(package.name, _SEMVER_COLOR_MAP.get(package.diff_type, cli.CLEAR)),
								package.current_version,
								_cli_format_semver(package.latest_version, package.diff_type)
							)

							if interactive:
								update = yes or cli.confirm(query)
								
							if update:
								cli.echo(cli_format(
									"Updating %s of %s %s: %s" % (
										i + 1,
										npackages,
										spackages,
										cli_format(package.name, cli.GREEN)
									)
								, cli.BOLD))

								_pip.install(package.name, user = user, quiet = not verbose, no_cache_dir = True, upgrade = True)
			else:
				cli.echo("%s upto date." % cli_format(stitle, cli.CYAN))

		if project and pull_request:
			if not git_username:
				raise ValueError('Git Username not found. Use --git-username or the environment variable "%s" to set value.' % getenvvar("GIT_USERNAME"))
			if not git_email:
				raise ValueError('Git Email not found. Use --git-email or the environment variable "%s" to set value.' % getenvvar("GIT_EMAIL"))

			for p in project:
				popen("git config user.name  %s" % git_username, cwd = p.path)
				popen("git config user.email %s" % git_email,    cwd = p.path)

				_, output, _ = popen("git status -s", output = True)

				if output:
					popen("git checkout -B %s" % get_timestamp_str(format_ = "%Y%m%d%H%M%S"))

					# TODO: cross-check with "git add" ?
					popen("git add %s" % " ".join(p.requirements), cwd = p.path)
					popen("git commit -m 'fix(dependencies): Update dependencies to latest.'", cwd = p.path)

					popen("git push", cwd = p.path)