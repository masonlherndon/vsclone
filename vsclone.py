
import os
import sys
import platform
import subprocess
import getpass
import shutil
import tempfile
import argparse
import json
import re
import uuid

PLATFORM_IDS: dict = {
	"installer_id": {
		"Linux": "linux-deb-x64",
		"Windows": "win32-x64-user",
	},
	"server_id": {
		"Linux": "linux-x64",
		"Windows": "win32-x64",
	},
	"extension_id": {
		"Linux": "linux-x64",
		"Windows": "win32-x64",
	},
}

def CurVSCodeVersion() -> str:
	output: str = subprocess.check_output("code --version", shell=True, text=True)
	return output.splitlines()[0]

def CurVSCodeCommitID() -> str:
	output: str = subprocess.check_output("code --version", shell=True, text=True)
	return output.splitlines()[1]

def CurVSCodeExtensions() -> list[str]:
	output: str = subprocess.check_output("code --list-extensions --show-versions", shell=True, text=True)
	return output.splitlines()

def ParseExtensionString(ext_str: str) -> tuple[str, str, str]:
	uid, version = ext_str.split("@")
	publisher, package = uid.split(".")
	return publisher, package, version

def InstallerURL(version: str, platform_id: str) -> str:
	return f"https://update.code.visualstudio.com/{version}/{platform_id}/stable"

def ServerURL(commit_id: str, platform_id: str) -> str:
	return f"https://update.code.visualstudio.com/commit:{commit_id}/server-{platform_id}/stable"

def ExtensionURL(publisher: str, package: str, version: str, platform_id: str | None = None, backup_api: bool = False) -> str:
	query_params = f"?targetPlatform={platform_id}" if platform_id else ""
	if not backup_api:
		return f"https://marketplace.visualstudio.com/_apis/public/gallery/publishers/{publisher}/vsextensions/{package}/{version}/vspackage{query_params}"
	else:
		return f"https://{publisher}.gallery.vsassets.io/_apis/public/gallery/publisher/{publisher}/extension/{package}/{version}/assetbyname/Microsoft.VisualStudio.Services.VSIXPackage{query_params}"

def ExtensionFilename(publisher: str, package: str, version: str, platform_id: str | None = None) -> str:
	if not platform_id:
		return f"{publisher}.{package}-{version}.vsix"
	else:
		return f"{publisher}.{package}-{version}@{platform_id}.vsix"


def GetFilenameFromHeaders(headers: dict) -> str:

	disposition: str = headers.get("content-disposition", default="")
	if not disposition:
		return ""

	match = re.search(r"filename=([^;]+)", disposition)
	if not match:
		return ""

	return match.group(1).strip("\'\"")

def Download(url: str, path: str = "") -> str:

	# These modules are only used during the "clone" step and aren't always in
	# the stdlib. Import them here so that the "install" step is
	# actually offline-friendly.
	import requests         # For the download itself.
	from tqdm import tqdm   # For the progress bar during the download.

	# Ask for file and return early if not found.
	response = requests.get(url, stream=True)
	if not response.ok:
		return ""

	header_name: str = GetFilenameFromHeaders(response.headers)

	# Figure out naming weirdness.
	display_name = os.path.basename(path) or header_name or url
	final_path = path or header_name or uuid.uuid4().hex

	file_size = int(response.headers.get("content-length", 0))

	# Write to disk as the download streams in (with a pretty progress bar).
	progress_bar = tqdm(desc=display_name, total=file_size, unit="iB", unit_scale=True, unit_divisor=1024)
	file = open(final_path, "wb")
	for data in response.iter_content(chunk_size=1024):
		num_bytes = file.write(data)
		progress_bar.update(num_bytes)
	file.close()
	progress_bar.close()

	return final_path

def Clone(dir: str) -> bool:

	os.chdir(dir)

	manifest: dict = {
		"version" : CurVSCodeVersion(),
		"commit_id" : CurVSCodeCommitID(),
		"installer" : {p : "" for p in PLATFORM_IDS["installer_id"].keys()},
		"server" : {p : "" for p in PLATFORM_IDS["server_id"].keys()},
		"extensions" : {},
	}

	# Get every variant of the installer.
	for platform in PLATFORM_IDS["installer_id"].keys():
		id = PLATFORM_IDS["installer_id"][platform]
		url = InstallerURL(CurVSCodeVersion(), id)
		path = Download(url)    # supplies its own name
		if path:
			manifest["installer"][platform] = path
		else:
			print(f"Error: Failed to get installer for {id}")
			return False

	# Get every variant of the server.
	for platform in PLATFORM_IDS["server_id"].keys():
		id = PLATFORM_IDS["server_id"][platform]
		url = ServerURL(CurVSCodeCommitID(), id)
		path = Download(url)    # supplies its own name
		if path:
			manifest["server"][platform] = path
		else:
			print(f"Error: Failed to get server for {id}")
			return False

	# Get every variant of every installed extension.
	for ext_str in CurVSCodeExtensions():

		publisher, package, version = ParseExtensionString(ext_str)
		got_all_platforms = True

		manifest["extensions"][ext_str] = {p : "" for p in PLATFORM_IDS["extension_id"].keys()} | {"Generic" : ""}

		# Try to get all of the platform specifc variants first.
		for platform in PLATFORM_IDS["extension_id"].keys():
			id = PLATFORM_IDS["extension_id"][platform]
			url = ExtensionURL(publisher, package, version, id, backup_api=True)
			path = Download(url, ExtensionFilename(publisher, package, version, id))
			if path:
				manifest["extensions"][ext_str][platform] = path
			else:
				got_all_platforms = False

		# If any platform variant failed, then we need the generic form too.
		if not got_all_platforms:
			url = ExtensionURL(publisher, package, version, backup_api=True)
			path = Download(url, ExtensionFilename(publisher, package, version))
			if path:
				manifest["extensions"][ext_str]["Generic"] = path
			else:
				print(f"Error: Failed to get extension {ext_str}")
				return False

	# Write the manifest file.
	with open("manifest.json", "w") as file:
		json.dump(manifest, file, indent="\t", sort_keys=True)

	return True

def ExecuteCommandArgv(cmd: list[str]) -> int:
	print(" ".join(cmd), flush=True)
	with subprocess.Popen(cmd, stdout=subprocess.PIPE) as p:
		while p.poll() is None:
			text = os.read(p.stdout.fileno(), 1024).decode("utf-8")
			print(text, end="", flush=True)
		return p.returncode

def ExecuteCommandStr(cmd: str) -> int:
	return ExecuteCommandArgv(cmd.split())

def Install(dir: str) -> bool:

	os.chdir(dir)

	with open("manifest.json", "r") as f:
		manifest: dict = json.loads(f.read())

	# Install new VSCode.
	installer = os.path.abspath(manifest["installer"][platform.system()])
	if platform.system() == "Linux":
		if ExecuteCommandStr(f"sudo apt-get install -y {installer}") != 0: return False
	elif platform.system() == "Windows":
		if ExecuteCommandStr(installer) != 0: return False
	else:
		print(f"Error: Installation not currently supported on {platform.system()}")
		return False

	# Collect extensions to be installed.
	to_install = []
	for ext_str in manifest["extensions"].keys():
		platform_vsix = manifest["extensions"][ext_str][platform.system()]
		generic_vsix = manifest["extensions"][ext_str]["Generic"]
		if platform_vsix:
			to_install.append(platform_vsix)
		elif generic_vsix:
			to_install.append(generic_vsix)
		else:
			print(f"Error: No compatible VSIX found for {ext_str} on your platform.")
			return False

	local_dir = os.path.expanduser("~/.vscode")
	local_extensions_dir = os.path.join(local_dir, "extensions")
	server_dir = os.path.expanduser("~/.vscode-server")
	server_extensions_dir = os.path.join(server_dir, "extensions")
	server_core_dir = os.path.join(server_dir, "bin", manifest["commit_id"])

	# Wipe old extension and server artifacts.
	shutil.rmtree(local_extensions_dir, ignore_errors=True)
	shutil.rmtree(server_dir, ignore_errors=True)

	# Install the new extensions.
	extension_args = []
	for vsix in [os.path.abspath(x) for x in to_install]:
		extension_args.append("--install-extension")
		extension_args.append(vsix)
	if platform.system() == "Windows":
		code_path = f"\"C:\\Users\\{getpass.getuser()}\\AppData\\Local\\Programs\\Microsoft VS Code\\bin\\code\""
		cmd = ["powershell", "Start-Process", "-verb", "runas", code_path, "\"" + " ".join(extension_args) + "\""]
	else:
		cmd = ["code"] + extension_args
	if ExecuteCommandArgv(cmd) != 0: return False

	# Extract/install the server and copy over local extensions
	archive = manifest["server"][platform.system()]
	with tempfile.TemporaryDirectory() as temp_dir:
		shutil.unpack_archive(archive, temp_dir)
		archive_payload = os.path.join(temp_dir, archive.split(".")[0])
		shutil.copytree(archive_payload, server_core_dir, dirs_exist_ok=True)
	shutil.copytree(local_extensions_dir, server_extensions_dir, dirs_exist_ok=True)

	return True

def main() -> int:

	description = f"""\
	VSClone -- VSCode offline installation/upgrade tool.

	VSClone helps you "clone" the VSCode setup from an internet-connected
	device onto an isolated (or not) device. The idea is that you can just let
	the internet-connected device auto-update itself, then periodically pull
	everything over.

	Clone (on source device):
		{os.path.basename(sys.executable)} %(prog)s -o DIR
			Download VSCode installer/server/extensions matching your current
			local versions and put them in DIR.

	Install (on destination device):
		{os.path.basename(sys.executable)} %(prog)s -i DIR
			Install/update device's VSCode with everything from DIR."""

	# Setup commandline option parser.
	parser = argparse.ArgumentParser(description=description, formatter_class=argparse.RawDescriptionHelpFormatter)
	group = parser.add_mutually_exclusive_group(required=True)
	group.add_argument("-o", dest="output_dir", help="Clone your current VSCode setup into the specified directory.")
	group.add_argument("-i", dest="input_dir", help="Install the VSCode setup from the specified directory.")

	args = parser.parse_args()

	if args.output_dir:

		# Make sure the directory exists.
		if not os.path.exists(args.output_dir):
			os.mkdir(args.output_dir)
		elif os.path.isfile(args.output_dir):
			print(f"Error: {args.output_dir} is not a directory")
			return -1
		success = Clone(args.output_dir)

	elif args.input_dir:

		# Make sure the directory exists.
		if not os.path.isdir(args.input_dir):
			print(f"Error: {args.input_dir} is not a directory")
			return -1
		success = Install(args.input_dir)

	if not success:
		print("Error: Something went wrong. Installation failed.")
		return -1

	return 0

if __name__ == "__main__":
	exit(main())

