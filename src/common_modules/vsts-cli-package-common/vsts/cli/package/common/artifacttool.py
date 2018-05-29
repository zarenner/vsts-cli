# --------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for license information.
# --------------------------------------------------------------------------------------------

import json
import logging
import os
import platform
import subprocess
import sys
import tempfile
import zipfile
import pathlib

import humanfriendly
import requests
import io
import zipfile

from knack.util import CLIError
from vsts.cli.common.services import _get_credentials

logger = logging.getLogger('vsts.packaging')

class ArtifactTool:
    PATVAR = "VSTS_ARTIFACTTOOL_PATVAR"
    ARTIFACTTOOL_OVERRIDE_PATH_ENVKEY = "VSTS_ARTIFACTTOOL_OVERRIDE_PATH"
    ARTIFACTTOOL_OVERRIDE_URL_ENVKEY = "VSTS_ARTIFACTTOOL_OVERRIDE_URL"

    def download_upack(self, team_instance, feed, package_name, package_version, path):
        proc = self.invoke_artifacttool(team_instance, ["upack", "download", "--service", team_instance, "--patvar", self.PATVAR, "--feed", feed, "--package-name", package_name, "--package-version", package_version, "--path", path])
        with humanfriendly.Spinner(label="Downloading...", total=100) as spinner:
            spinner.step()
            self._process_stderr(proc, spinner)
        self._check_proc_result(proc)

    def publish_upack(self, team_instance, feed, package_name, package_version, description, path):
        args = ["upack", "publish", "--service", team_instance, "--patvar", self.PATVAR, "--feed", feed, "--package-name", package_name, "--package-version", package_version, "--path", path]
        if description:
            args.extend(["--description", description])
        proc = self.invoke_artifacttool(team_instance, args)
        
        with humanfriendly.Spinner(label="Publishing...", total=100) as spinner:
            spinner.step()
            self._process_stderr(proc, spinner)
        self._check_proc_result(proc)

    def invoke_artifacttool(self, team_instance, args):
        # Determine ArtifactTool binary path
        artifacttool_binary_path = r"C:\repos\ArtifactTool\src\ArtifactTool\bin\Debug\netcoreapp2.0\Win10-x64\ArtifactTool.exe" # TODO hook into auto-update flow
        artifacttool_binary_override_path = os.environ.get(self.ARTIFACTTOOL_OVERRIDE_PATH_ENVKEY)
        if artifacttool_binary_override_path is not None:
            artifacttool_binary_path = artifacttool_binary_override_path
            logger.debug("ArtifactTool path was overriden to '%s' due to environment variable %s" % (artifacttool_binary_path, self.ARTIFACTTOOL_OVERRIDE_PATH_ENVKEY))
        else:
            artifacttool_binary_path = self._get_artifacttool(team_instance)
            logger.debug("Using downloaded ArtifactTool from '%s'" % artifacttool_binary_path)

        # Populate the environment for the process with the PAT
        creds = _get_credentials(team_instance)
        new_env = os.environ.copy()
        new_env[self.PATVAR] = creds.password

        # Run ArtifactTool
        command_args = [artifacttool_binary_path] + args
        logger.debug("Running ArtifactTool command: %s" % ' '.join(command_args))

        proc = subprocess.Popen(
            command_args,
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=new_env)
        return proc

    def _process_stderr(self, proc, spinner):
        for line in proc.stderr:
            text_line = str(line, 'utf-8').strip()            

            try:
                json_line = json.loads(text_line)
            except:
                json_line = None
                logger.debug("Failed to parse JSON log line. Ensure that ArtifactTool structured logging is enabled.")

            if json_line is not None and '@m' in json_line:
                log_level = json_line['@l'] if '@l' in json_line else "Information" # Serilog doesn't emit @l for Information, it seems
                message = json_line['@m']
                if log_level in ["Critical", "Error"]:
                    raise CLIError(message)
                elif log_level == "Warning":
                    logger.warning(message)
                elif log_level == "Information":
                    logger.info(message)
                else:
                    logger.debug(message)
            else:          
                logger.debug(text_line)
                    

            if json_line and 'EventId' in json_line and 'Name' in json_line['EventId']:
                event_name = json_line['EventId']['Name']

                if event_name == "ProcessingFiles":
                    processed_files = json_line['ProcessedFiles']
                    total_files = json_line['TotalFiles']
                    percent = 100 * float(processed_files) / float(total_files)
                    spinner.step(progress=percent, label="Pre-upload processing: %s/%s files" % (processed_files, total_files))

                if event_name == "Uploading":
                    uploaded_bytes = json_line['UploadedBytes']
                    total_bytes = json_line['TotalBytes']
                    percent = 100 * float(uploaded_bytes) / float(total_bytes)
                    spinner.step(progress=percent, label="Uploading: %s/%s bytes" % (uploaded_bytes, total_bytes))

    def _check_proc_result(self, proc):
        # Ensure process completed
        proc.wait()
        if proc.returncode != 0:
            stderr = str(proc.stderr.read(), 'utf-8').strip()
            if stderr != "":
                stderr = "\n" + stderr
            raise Exception("ArtifactTool exited with return code %i%s" % (proc.returncode, stderr))

    ### Auto-update
    def _get_artifacttool(self, team_instance):
        logger.debug("Checking for ArtifactTool updates")
        artifacttool_binary_url = "https://zachtest1.blob.core.windows.net/test/artifacttool-win10-x64-Release.zip"
        artifacttool_binary_override_url = os.environ.get(self.ARTIFACTTOOL_OVERRIDE_URL_ENVKEY)
        if artifacttool_binary_override_url is not None:
            artifacttool_binary_url = artifacttool_binary_override_url
            logger.debug("ArtifactTool download URL was overridden to '%s' due to environment variable %s" % (artifacttool_binary_override_url, self.ARTIFACTTOOL_OVERRIDE_URL_ENVKEY))
        
        head_result = requests.head(artifacttool_binary_url)
        etag = head_result.headers.get('ETag').strip("\"").replace("0x", "").lower()

        temp_dir = tempfile.gettempdir()
        tool_root = os.path.join(temp_dir, "ArtifactTool")
        tool_dir = os.path.join(tool_root, etag)
          
        # For now, just download if the directory for this etag doesn't exist
        if not os.path.exists(tool_dir):
            content = requests.get(artifacttool_binary_url)
            f = zipfile.ZipFile(io.BytesIO(content.content))
            f.extractall(path=tool_dir)

        return os.path.join(tool_dir, "artifacttool-win10-x64-Release", "ArtifactTool.exe")



