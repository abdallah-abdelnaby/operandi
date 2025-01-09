from json import loads
from logging import getLogger
import signal
from os import getpid, getppid, setsid
from pathlib import Path
from sys import exit
from typing import List

from ocrd import Resolver
from operandi_utils import reconfigure_all_loggers, get_log_file_path_prefix
from operandi_utils.constants import LOG_LEVEL_WORKER, StateJob, StateWorkspace
from operandi_utils.database import (
    DBHPCSlurmJob, DBWorkflowJob, DBWorkspace,
    sync_db_increase_processing_stats, sync_db_initiate_database, sync_db_get_hpc_slurm_job, sync_db_get_workflow_job,
    sync_db_get_workspace, sync_db_update_hpc_slurm_job, sync_db_update_workflow_job, sync_db_update_workspace)
from operandi_utils.hpc import NHRExecutor, NHRTransfer
from operandi_utils.rabbitmq import get_connection_consumer


# TODO: Refactor status worker to not download files. Adapt the JobDownloadWorker to do the task.
class JobStatusWorker:
    def __init__(self, db_url, rabbitmq_url, queue_name, tunnel_port_executor, tunnel_port_transfer, test_sbatch=False):
        self.log = getLogger(f"operandi_broker.job_status_worker[{getpid()}].{queue_name}")
        self.queue_name = queue_name
        self.log_file_path = f"{get_log_file_path_prefix(module_type='worker')}_{queue_name}.log"
        self.test_sbatch = test_sbatch

        self.db_url = db_url
        self.rmq_url = rabbitmq_url
        self.rmq_consumer = None
        self.hpc_executor = None
        self.hpc_io_transfer = None

        # Currently consumed message related parameters
        self.current_message_delivery_tag = None
        self.current_message_job_id = None
        self.has_consumed_message = False

        self.tunnel_port_executor = tunnel_port_executor
        self.tunnel_port_transfer = tunnel_port_transfer

    def __del__(self):
        if self.rmq_consumer:
            self.rmq_consumer.disconnect()

    def run(self):
        try:
            # Source: https://unix.stackexchange.com/questions/18166/what-are-session-leaders-in-ps
            # Make the current process session leader
            setsid()
            # Reconfigure all loggers to the same format
            reconfigure_all_loggers(log_level=LOG_LEVEL_WORKER, log_file_path=self.log_file_path)
            self.log.info(f"Activating signal handler for SIGINT, SIGTERM")
            signal.signal(signal.SIGINT, self.signal_handler)
            signal.signal(signal.SIGTERM, self.signal_handler)

            sync_db_initiate_database(self.db_url)
            self.hpc_executor = NHRExecutor()
            self.log.info("HPC executor connection successful.")
            self.hpc_io_transfer = NHRTransfer()
            self.log.info("HPC transfer connection successful.")

            self.rmq_consumer = get_connection_consumer(rabbitmq_url=self.rmq_url)
            self.log.info(f"RMQConsumer connected")
            self.rmq_consumer.configure_consuming(queue_name=self.queue_name, callback_method=self.__callback)
            self.log.info(f"Configured consuming from queue: {self.queue_name}")
            self.log.info(f"Starting consuming from queue: {self.queue_name}")
            self.rmq_consumer.start_consuming()
        except Exception as e:
            self.log.error(f"The worker failed, reason: {e}")
            raise Exception(f"The worker failed, reason: {e}")

    def __download_results_from_hpc(self, job_dir: str, workspace_dir: str) -> None:
        self.hpc_io_transfer.get_and_unpack_slurm_workspace(
            ocrd_workspace_dir=Path(workspace_dir), workflow_job_dir=Path(job_dir))
        self.log.info(f"Transferred slurm workspace from hpc path")
        # Delete the result dir from the HPC home folder
        # self.hpc_executor.execute_blocking(f"bash -lc 'rm -rf {hpc_slurm_workspace_path}/{workflow_job_id}'")

    def __extract_updated_file_groups(self, db_workspace: DBWorkspace) -> List[str]:
        try:
            workspace = Resolver().workspace_from_url(
                mets_url=db_workspace.workspace_mets_path, clobber_mets=False,
                mets_basename=db_workspace.mets_basename, download=False)
            return workspace.mets.file_groups
        except Exception as error:
            self.log.error(f"Failed to extract the processed file groups: {error}")
            return ["CORRUPTED FILE GROUPS"]

    def __handle_hpc_and_workflow_states(
        self, hpc_slurm_job_db: DBHPCSlurmJob, workflow_job_db: DBWorkflowJob, workspace_db: DBWorkspace
    ):
        old_slurm_job_state = hpc_slurm_job_db.hpc_slurm_job_state
        new_slurm_job_state = self.hpc_executor.check_slurm_job_state(slurm_job_id=hpc_slurm_job_db.hpc_slurm_job_id)
        # TODO: Reconsider this
        # if not new_slurm_job_state:
        #   return

        job_id = workflow_job_db.job_id
        job_dir = workflow_job_db.job_dir
        old_job_state = workflow_job_db.job_state

        workspace_id = workspace_db.workspace_id
        workspace_dir = workspace_db.workspace_dir

        # If there has been a change of slurm job state, update it
        if old_slurm_job_state != new_slurm_job_state:
            self.log.info(
                f"Slurm job: {hpc_slurm_job_db.hpc_slurm_job_id}, "
                f"old state: {old_slurm_job_state}, "
                f"new state: {new_slurm_job_state}")
            sync_db_update_hpc_slurm_job(find_workflow_job_id=job_id, hpc_slurm_job_state=new_slurm_job_state)

        # Convert the slurm job state to operandi workflow job state
        new_job_state = StateJob.convert_from_slurm_job(slurm_job_state=new_slurm_job_state)

        # TODO: Refactor this block of code since nothing is downloaded from the HPC when job fails.
        # If there has been a change of operandi workflow state, update it
        if old_job_state != new_job_state:
            self.log.info(f"Workflow job id: {job_id}, old state: {old_job_state}, new state: {new_job_state}")
            if new_job_state == StateJob.SUCCESS:
                self.hpc_io_transfer.download_slurm_job_log_file(hpc_slurm_job_db.hpc_slurm_job_id, job_dir)
                sync_db_update_workspace(find_workspace_id=workspace_id, state=StateWorkspace.TRANSFERRING_FROM_HPC)
                sync_db_update_workflow_job(find_job_id=job_id, job_state=StateJob.TRANSFERRING_FROM_HPC)
                self.__download_results_from_hpc(job_dir=job_dir, workspace_dir=workspace_dir)

                self.log.info(f"Setting new workspace state `{StateWorkspace.READY}` of workspace_id: {workspace_id}")
                updated_file_groups = self.__extract_updated_file_groups(db_workspace=workspace_db)
                db_workspace = sync_db_update_workspace(
                    find_workspace_id=workspace_id, state=StateWorkspace.READY, file_groups=updated_file_groups)

                self.log.info(f"Setting new workflow job state `{StateJob.SUCCESS}` of job_id: {job_id}")
                sync_db_update_workflow_job(find_job_id=self.current_message_job_id, job_state=StateJob.SUCCESS)

                self.log.info(f"Increasing `pages_succeed` stat by {db_workspace.pages_amount}")
                db_stats = sync_db_increase_processing_stats(
                    find_user_id=workspace_db.user_id, pages_succeed=db_workspace.pages_amount)
                self.log.info(f"Total amount of `pages_succeed` stat: {db_stats.pages_succeed}")

            if new_job_state == StateJob.FAILED:
                self.hpc_io_transfer.download_slurm_job_log_file(hpc_slurm_job_db.hpc_slurm_job_id, job_dir)
                self.log.info(f"Setting new workspace state `{StateWorkspace.READY}` of workspace_id: {workspace_id}")
                db_workspace = sync_db_update_workspace(find_workspace_id=workspace_id, state=StateWorkspace.READY)

                self.log.info(f"Setting new workflow job state `{StateJob.FAILED}` of job_id: {job_id}")
                sync_db_update_workflow_job(find_job_id=self.current_message_job_id, job_state=StateJob.FAILED)

                self.log.error(f"Increasing `pages_failed` stat by {db_workspace.pages_amount}")
                db_stats = sync_db_increase_processing_stats(
                    find_user_id=workspace_db.user_id, pages_failed=db_workspace.pages_amount)
                self.log.error(f"Total amount of `pages_failed` stat: {db_stats.pages_failed}")

        self.log.info(f"Latest slurm job state: {new_slurm_job_state}")
        self.log.info(f"Latest workflow job state: {new_job_state}")

    def __callback(self, ch, method, properties, body):
        self.log.debug(f"ch: {ch}, method: {method}, properties: {properties}, body: {body}")
        self.log.debug(f"Consumed message: {body}")

        self.current_message_delivery_tag = method.delivery_tag
        self.has_consumed_message = True

        # Since the workflow_message is constructed by the Operandi Server,
        # it should not fail here when parsing under normal circumstances.
        try:
            consumed_message = loads(body)
            self.log.info(f"Consumed message: {consumed_message}")
            self.current_message_job_id = consumed_message["job_id"]
        except Exception as error:
            self.log.warning(f"Parsing the consumed message has failed: {error}")
            self.__handle_message_failure(interruption=False)
            return

        # Handle database related reads and set the workflow job status to RUNNING
        try:
            db_workflow_job = sync_db_get_workflow_job(self.current_message_job_id)
            db_workspace = sync_db_get_workspace(db_workflow_job.workspace_id)
            db_hpc_slurm_job = sync_db_get_hpc_slurm_job(self.current_message_job_id)
        except RuntimeError as error:
            self.log.warning(f"Database run-time error has occurred: {error}")
            self.__handle_message_failure(interruption=False)
            return
        except Exception as error:
            self.log.warning(f"Database related error has occurred: {error}")
            self.__handle_message_failure(interruption=False)
            return

        try:
            self.__handle_hpc_and_workflow_states(
                hpc_slurm_job_db=db_hpc_slurm_job, workflow_job_db=db_workflow_job, workspace_db=db_workspace)
        except ValueError as error:
            self.log.warning(f"{error}")
            self.__handle_message_failure(interruption=False)
            return

        self.has_consumed_message = False
        self.log.debug(f"Ack delivery tag: {self.current_message_delivery_tag}")
        ch.basic_ack(delivery_tag=method.delivery_tag)

    def __handle_message_failure(self, interruption: bool = False):
        self.has_consumed_message = False

        if interruption:
            # self.log.info(f"Nacking delivery tag: {self.current_message_delivery_tag}")
            # self.rmq_consumer._channel.basic_nack(delivery_tag=self.current_message_delivery_tag)
            # TODO: Sending ACK for now because it is hard to clean up without a mets workspace backup mechanism
            self.log.info(f"Interruption ack delivery tag: {self.current_message_delivery_tag}")
            self.rmq_consumer.ack_message(delivery_tag=self.current_message_delivery_tag)
            return

        self.log.debug(f"Ack delivery tag: {self.current_message_delivery_tag}")
        self.rmq_consumer.ack_message(delivery_tag=self.current_message_delivery_tag)

        # Reset the current message related parameters
        self.current_message_delivery_tag = None
        self.current_message_job_id = None

    # TODO: Ideally this method should be wrapped to be able
    #  to pass internal data from the Worker class required for the cleaning
    # The arguments to this method are passed by the caller from the OS
    def signal_handler(self, sig, frame):
        signal_name = signal.Signals(sig).name
        self.log.info(f"{signal_name} received from parent process `{getppid()}`.")
        if self.has_consumed_message:
            self.log.info(f"Handling the message failure due to interruption: {signal_name}")
            self.__handle_message_failure(interruption=True)
        self.rmq_consumer.disconnect()
        self.rmq_consumer = None
        self.log.info("Exiting gracefully.")
        exit(0)
