from json import loads
from logging import getLogger
import signal
from os import getpid, getppid, setsid
from os.path import join
from pathlib import Path
from sys import exit
from typing import List

from operandi_utils import reconfigure_all_loggers, get_log_file_path_prefix
from operandi_utils.constants import LOG_LEVEL_WORKER, StateJob, StateWorkspace
from operandi_utils.database import (
    sync_db_increase_processing_stats, sync_db_initiate_database, sync_db_get_workflow, sync_db_get_workspace,
    sync_db_create_hpc_slurm_job, sync_db_update_workflow_job, sync_db_update_workspace)
from operandi_utils.hpc import NHRExecutor, NHRTransfer
from operandi_utils.hpc.constants import (
    HPC_BATCH_SUBMIT_WORKFLOW_JOB, HPC_JOB_DEADLINE_TIME_REGULAR, HPC_JOB_DEADLINE_TIME_TEST, HPC_JOB_QOS_SHORT,
    HPC_JOB_QOS_DEFAULT)
from operandi_utils.rabbitmq import get_connection_consumer


# Each worker class listens to a specific queue,
# consume messages, and process messages.
class JobSubmitWorker:
    def __init__(self, db_url, rabbitmq_url, queue_name, tunnel_port_executor, tunnel_port_transfer, test_sbatch=False):
        self.log = getLogger(f"operandi_broker.worker[{getpid()}].{queue_name}")
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
        self.current_message_user_id = None
        self.current_message_ws_id = None
        self.current_message_wf_id = None
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
            self.current_message_user_id = consumed_message["user_id"]
            self.current_message_ws_id = consumed_message["workspace_id"]
            self.current_message_wf_id = consumed_message["workflow_id"]
            self.current_message_job_id = consumed_message["job_id"]
            input_file_grp = consumed_message["input_file_grp"]
            remove_file_grps = consumed_message["remove_file_grps"]
            slurm_job_partition = consumed_message["partition"]
            slurm_job_cpus = int(consumed_message["cpus"])
            slurm_job_ram = int(consumed_message["ram"])
            # How many process instances to create for each OCR-D processor
            # By default, the amount of cpus, since that gives optimal performance
            nf_process_forks = slurm_job_cpus
        except Exception as error:
            self.log.error(f"Parsing the consumed message has failed: {error}")
            self.__handle_message_failure(interruption=False)
            return

        # Handle database related reads and set the workflow job status to RUNNING
        try:
            workflow_db = sync_db_get_workflow(self.current_message_wf_id)
            workspace_db = sync_db_get_workspace(self.current_message_ws_id)

            workflow_script_path = Path(workflow_db.workflow_script_path)
            nf_uses_mets_server = workflow_db.uses_mets_server
            nf_executable_steps = workflow_db.executable_steps
            workspace_dir = Path(workspace_db.workspace_dir)
            mets_basename = workspace_db.mets_basename
            ws_pages_amount = workspace_db.pages_amount
            if not mets_basename:
                mets_basename = "mets.xml"
        except RuntimeError as error:
            self.log.error(f"Database run-time error has occurred: {error}")
            self.__handle_message_failure(interruption=False, set_ws_ready=True)
            return
        except Exception as error:
            self.log.error(f"Database related error has occurred: {error}")
            self.__handle_message_failure(interruption=False, set_ws_ready=True)
            return

        # Trigger a slurm job in the HPC
        try:
            self.prepare_and_trigger_slurm_job(
                workflow_job_id=self.current_message_job_id, workspace_id=self.current_message_ws_id,
                workspace_dir=workspace_dir, workspace_base_mets=mets_basename,
                workflow_script_path=workflow_script_path, input_file_grp=input_file_grp,
                nf_process_forks=nf_process_forks, ws_pages_amount=ws_pages_amount, use_mets_server=nf_uses_mets_server,
                nf_executable_steps=nf_executable_steps, file_groups_to_remove=remove_file_grps, cpus=slurm_job_cpus,
                ram=slurm_job_ram, partition=slurm_job_partition
            )
            self.log.info(f"The HPC slurm job was successfully submitted")
        except Exception as error:
            self.log.error(f"Triggering a slurm job in the HPC has failed: {error}")
            self.__handle_message_failure(interruption=False, set_ws_ready=True)
            return

        job_state = StateJob.PENDING
        self.log.info(f"Setting new job state `{job_state}` of job_id: {self.current_message_job_id}")
        sync_db_update_workflow_job(find_job_id=self.current_message_job_id, job_state=job_state)

        ws_state = StateWorkspace.PENDING
        self.log.info(f"Setting new workspace state `{ws_state}` of workspace_id: {self.current_message_ws_id}")
        sync_db_update_workspace(find_workspace_id=self.current_message_ws_id, state=ws_state)

        self.has_consumed_message = False
        self.log.debug(f"Ack delivery tag: {self.current_message_delivery_tag}")
        ch.basic_ack(delivery_tag=method.delivery_tag)

    def __handle_message_failure(self, interruption: bool = False, set_ws_ready: bool = False):
        job_state = StateJob.FAILED
        self.log.info(f"Setting new state `{job_state}` of job_id: {self.current_message_job_id}")
        sync_db_update_workflow_job(find_job_id=self.current_message_job_id, job_state=job_state)
        self.has_consumed_message = False

        if set_ws_ready:
            ws_state = StateWorkspace.READY
            self.log.info(f"Setting new workspace state `{ws_state}` of workspace_id: {self.current_message_ws_id}")
            sync_db_update_workspace(find_workspace_id=self.current_message_ws_id, state=ws_state)

        if interruption:
            # self.log.info(f"Nacking delivery tag: {self.current_message_delivery_tag}")
            # self.rmq_consumer._channel.basic_nack(delivery_tag=self.current_message_delivery_tag)
            # TODO: Sending ACK for now because it is hard to clean up without a mets workspace backup mechanism
            self.log.info(f"Interruption Ack delivery tag: {self.current_message_delivery_tag}")
            self.rmq_consumer.ack_message(delivery_tag=self.current_message_delivery_tag)
            return

        self.log.debug(f"Ack delivery tag: {self.current_message_delivery_tag}")
        self.rmq_consumer.ack_message(delivery_tag=self.current_message_delivery_tag)

        # Reset the current message related parameters
        self.current_message_delivery_tag = None
        self.current_message_ws_id = None
        self.current_message_wf_id = None
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

    # TODO: This should be further refined, currently it's just everything in one place
    def prepare_and_trigger_slurm_job(
        self, workflow_job_id: str, workspace_id: str, workspace_dir: Path, workspace_base_mets: str,
        workflow_script_path: Path, input_file_grp: str, nf_process_forks: int, ws_pages_amount: int,
        use_mets_server: bool, nf_executable_steps: List[str], file_groups_to_remove: str, cpus: int, ram: int,
        partition: str
    ) -> str:
        if self.test_sbatch:
            job_deadline_time = HPC_JOB_DEADLINE_TIME_TEST
            qos = HPC_JOB_QOS_SHORT
        else:
            job_deadline_time = HPC_JOB_DEADLINE_TIME_REGULAR
            qos = HPC_JOB_QOS_DEFAULT

        # Recreate the transfer connection for each workflow job submission
        # This is required due to all kind of nasty connection fails - timeouts,
        # paramiko transport not reporting properly, etc.
        # self.hpc_executor = HPCExecutor(tunel_host='localhost', tunel_port=4022)
        # self.log.info("HPC executor connection renewed successfully.")
        # self.hpc_io_transfer = HPCTransfer(tunel_host='localhost', tunel_port=4023)
        # self.log.info("HPC transfer connection renewed successfully.")

        try:
            sync_db_update_workspace(find_workspace_id=workspace_id, state=StateWorkspace.TRANSFERRING_TO_HPC)
            sync_db_update_workflow_job(find_job_id=workflow_job_id, job_state=StateJob.TRANSFERRING_TO_HPC)
            self.hpc_io_transfer.pack_and_put_slurm_workspace(
                ocrd_workspace_dir=workspace_dir, workflow_job_id=workflow_job_id,
                nextflow_script_path=workflow_script_path)
        except Exception as error:
            raise Exception(f"Failed to pack and put slurm workspace: {error}")

        try:
            # NOTE: The paths below must be a valid existing path inside the HPC
            slurm_job_id = self.hpc_executor.trigger_slurm_job(
                workflow_job_id=workflow_job_id, nextflow_script_path=workflow_script_path,
                workspace_id=workspace_id, mets_basename=workspace_base_mets,
                input_file_grp=input_file_grp, nf_process_forks=nf_process_forks, ws_pages_amount=ws_pages_amount,
                use_mets_server=use_mets_server, nf_executable_steps=nf_executable_steps,
                file_groups_to_remove=file_groups_to_remove, cpus=cpus, ram=ram, job_deadline_time=job_deadline_time,
                partition=partition, qos=qos)
        except Exception as error:
            db_stats = sync_db_increase_processing_stats(
                find_user_id=self.current_message_user_id, pages_failed=ws_pages_amount)
            self.log.error(f"Increasing `pages_failed` stat by {ws_pages_amount}")
            self.log.error(f"Total amount of `pages_failed` stat: {db_stats.pages_failed}")
            raise Exception(f"Triggering slurm job failed: {error}")

        try:
            sync_db_create_hpc_slurm_job(
                user_id=self.current_message_user_id,
                workflow_job_id=workflow_job_id, hpc_slurm_job_id=slurm_job_id,
                hpc_batch_script_path=HPC_BATCH_SUBMIT_WORKFLOW_JOB,
                hpc_slurm_workspace_path=join(self.hpc_io_transfer.slurm_workspaces_dir, workflow_job_id))
        except Exception as error:
            raise Exception(f"Failed to save the hpc slurm job in DB: {error}")
        return slurm_job_id
