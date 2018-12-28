#!/usr/bin/env python3
#
# Copyright (c) Bo Peng and the University of Texas MD Anderson Cancer Center
# Distributed under the terms of the 3-clause BSD License.

import copy
import os
import subprocess
import sys
import time
import zmq
import ast

from collections import Iterable, Mapping, Sequence, defaultdict
from typing import List, Union

from .eval import SoS_eval, SoS_exec, accessed_vars
from .syntax import (SOS_DEPENDS_OPTIONS, SOS_INPUT_OPTIONS, SOS_TARGETS_OPTIONS,
                     SOS_OUTPUT_OPTIONS, SOS_RUNTIME_OPTIONS)
from .targets import (RemovedTarget, RuntimeInfo, UnavailableLock,
                      UnknownTarget, dynamic, file_target,
                      sos_targets, sos_step)
from .tasks import MasterTaskParams, TaskFile
from .utils import (StopInputGroup, TerminateExecution, ArgumentError, env,
                    expand_size, format_HHMMSS, get_traceback, short_repr)
from .executor_utils import (clear_output, create_task, verify_input, reevaluate_output,
                    validate_step_sig, statementMD5, get_traceback_msg, __null_func__,
                    __output_from__, __named_output__)


__all__ = []


class TaskManager:
    # manage tasks created by the step
    def __init__(self, trunk_size, trunk_workers):
        super(TaskManager, self).__init__()
        self.trunk_size = trunk_size
        self.trunk_workers = trunk_workers
        self._submitted_tasks = []
        self._unsubmitted_tasks = []
        # derived from _unsubmitted_tasks
        self._all_ids = []
        self._all_output = []
        #
        self._terminate = False
        #
        self._tags = {}

    def append(self, task_def):
        self._unsubmitted_tasks.append(task_def)
        if isinstance(task_def[2], Sequence):
            self._all_output.extend(task_def[2])
        self._all_ids.append(task_def[0])
        self._tags[task_def[0]] = task_def[1].tags

    def tags(self, task_id):
        return self._tags.get(task_id, [])

    def index_of(self, task_id):
        if task_id in self._all_ids:
            return self._all_ids.index(task_id)
        else:
            return -1

    def has_output(self, output):
        if not isinstance(output, Sequence) or not self._unsubmitted_tasks:
            return False
        return any(x in self._all_output for x in output)

    def get_job(self, all_tasks=False):
        # save tasks
        if not self._unsubmitted_tasks:
            return None
        # single tasks
        if self.trunk_size == 1 or all_tasks:
            to_be_submitted = self._unsubmitted_tasks
            self._unsubmitted_tasks = []
        else:
            # save complete blocks
            num_tasks = len(
                self._unsubmitted_tasks) // self.trunk_size * self.trunk_size
            to_be_submitted = self._unsubmitted_tasks[: num_tasks]
            self._unsubmitted_tasks = self._unsubmitted_tasks[num_tasks:]

        # save tasks
        ids = []
        if self.trunk_size == 1 or (all_tasks and len(self._unsubmitted_tasks) == 1):
            for task_id, taskdef, _ in to_be_submitted:
                # if the task file, perhaps it is already running, we do not change
                # the task file. Otherwise we are changing the status of the task
                TaskFile(task_id).save(taskdef)
                env.signature_push_socket.send_pyobj(['workflow', 'task', task_id,
                                          f"{{'creation_time': {time.time()}}}"])
                ids.append(task_id)
        else:
            master = None
            for task_id, taskdef, _ in to_be_submitted:
                if master is not None and master.num_tasks() == self.trunk_size:
                    ids.append(master.ID)
                    TaskFile(master.ID).save(master)
                    env.signature_push_socket.send_pyobj(['workflow', 'task', master.ID,
                                              f"{{'creation_time': {time.time()}}}"])
                    master = None
                if master is None:
                    master = MasterTaskParams(self.trunk_workers)
                master.push(task_id, taskdef)
            # the last piece
            if master is not None:
                TaskFile(master.ID).save(master)
                env.signature_push_socket.send_pyobj(['workflow', 'task', master.ID,
                                          f"{{'creation_time': {time.time()}}}"])
                ids.append(master.ID)

        if not ids:
            return None

        self._submitted_tasks.extend(ids)
        return ids

    def clear_submitted(self):
        self._submitted_tasks = []


def expand_input_files(value, *args, **kwargs):
    # if unspecified, use __step_output__ as input (default)
    # resolve dynamic input.
    args = [x.resolve() if isinstance(x, dynamic) else x for x in args]
    kwargs = {x:(y.resolve() if isinstance(y, dynamic) else y) for x,y in kwargs.items()}

    # if no input,
    if not args and not kwargs:
        return env.sos_dict['step_input']
    # if only group_by ...
    elif not args and all(x in SOS_TARGETS_OPTIONS for x in kwargs.keys()):
        return sos_targets(env.sos_dict['step_input'], **kwargs)
    else:
        return sos_targets(*args, **kwargs, _verify_existence=True, _undetermined=False, _source=env.sos_dict['step_name'])

def expand_depends_files(*args, **kwargs):
    '''handle directive depends'''
    args = [x.resolve() if isinstance(x, dynamic) else x for x in args]
    kwargs = {x:(y.resolve() if isinstance(y, dynamic) else y) for x,y in kwargs.items()}
    return sos_targets(*args, **kwargs, _verify_existence=True, _undetermined=False, _source=env.sos_dict['step_name'])

def expand_output_files(value, *args, **kwargs):
    '''Process output files (perhaps a pattern) to determine input files.
    '''
    if any(isinstance(x, dynamic) for x in args) or any(isinstance(y, dynamic) for y in kwargs.values()):
        return sos_targets(_undetermined=value)
    else:
        return sos_targets(*args, **kwargs, _undetermined=False, _source=env.sos_dict['step_name'])


def parse_shared_vars(option):
    shared_vars = set()
    if not option:
        return shared_vars
    if isinstance(option, str):
        shared_vars.add(option)
    elif isinstance(option, Mapping):
        for var, val in option.items():
            shared_vars |= accessed_vars(val)
    elif isinstance(option, Sequence):
        for item in option:
            if isinstance(item, str):
                shared_vars.add(item)
            elif isinstance(item, Mapping):
                for var, val in item.items():
                    shared_vars |= accessed_vars(val)
    return shared_vars

def evaluate_shared(vars, option):
    # handle option shared and store variables in a "__shared_vars" variable
    shared_vars = {}
    env.sos_dict.quick_update(vars[-1])
    for key in vars[-1].keys():
        try:
            if key in ('output', 'depends', 'input'):
                env.logger.warning(f'Cannot overwrite variable step_{key} from substep variable {key}')
            else:
                env.sos_dict.set('step_' + key, [x[key] for x in vars])
        except Exception as e:
            env.logger.warning(f'Failed to create step level variable step_{key}: {e}')
    if isinstance(option, str):
        if option in env.sos_dict:
            shared_vars[option] = env.sos_dict[option]
        else:
            raise RuntimeError(f'shared variable does not exist: {option}')
    elif isinstance(option, Mapping):
        for var, val in option.items():
            try:
                if var == val:
                    shared_vars[var] = env.sos_dict[var]
                else:
                    shared_vars[var] = SoS_eval(val)
            except Exception as e:
                raise RuntimeError(
                    f'Failed to evaluate shared variable {var} from expression {val}: {e}')
    # if there are dictionaries in the sequence, e.g.
    # shared=['A', 'B', {'C':'D"}]
    elif isinstance(option, Sequence):
        for item in option:
            if isinstance(item, str):
                if item in env.sos_dict:
                    shared_vars[item] = env.sos_dict[item]
                else:
                    raise RuntimeError(f'shared variable does not exist: {option}')
            elif isinstance(item, Mapping):
                for var, val in item.items():
                    try:
                        if var == val:
                            continue
                        else:
                            shared_vars[var] = SoS_eval(val)
                    except Exception as e:
                        raise RuntimeError(
                            f'Failed to evaluate shared variable {var} from expression {val}: {e}')
            else:
                raise RuntimeError(f'Unacceptable shared option. Only str or mapping are accepted in sequence: {option}')
    else:
        raise RuntimeError(f'Unacceptable shared option. Only str, sequence, or mapping are accepted in sequence: {option}')
    return shared_vars


def get_value_of_param(name, param_list, extra_dict={}):
    tree = ast.parse(f'__null_func__({param_list})')
    # x.func can be an attribute (e.g. a.b()) and do not have id
    kwargs = [x for x in ast.walk(tree) if x.__class__.__name__ == 'keyword' and x.arg == name]
    if not kwargs:
        return []
    try:
        return [ast.literal_eval(kwargs[0].value)]
    except Exception as e:
        return [eval(compile(ast.Expression(body=kwargs[0].value), filename='<string>', mode="eval"), extra_dict)]

class Base_Step_Executor:
    # This base class defines how steps are executed. The derived classes will reimplement
    # some function to behave differently in different modes.
    #
    def __init__(self, step):
        self.step = step
        self.task_manager = None

    def verify_output(self):
        if env.sos_dict['step_output'] is None:
            return
        if not env.sos_dict['step_output'].valid():
            raise RuntimeError(
                'Output of a completed step cannot be undetermined or unspecified.')
        for target in env.sos_dict['step_output']:
            if isinstance(target, sos_step):
                continue
            if isinstance(target, str):
                if not file_target(target).target_exists('any'):
                    if env.config['run_mode'] == 'dryrun':
                        # in dryrun mode, we just create these targets
                        file_target(target).create_placeholder()
                    else:
                        # latency wait for 5 seconds because the file system might be slow
                        time.sleep(5)
                        if not file_target(target).target_exists('any'):
                            raise RuntimeError(
                                f'Output target {target} does not exist after the completion of step {env.sos_dict["step_name"]} (curdir={os.getcwd()})')
            elif not target.target_exists('any'):
                if env.config['run_mode'] == 'dryrun':
                    target.create_placeholder()
                else:
                    time.sleep(5)
                    if not target.target_exists('any'):
                        raise RuntimeError(
                            f'Output target {target} does not exist after the completion of step {env.sos_dict["step_name"]}')


    # directive input
    def process_input_args(self, ifiles: sos_targets, **kwargs):
        """This function handles directive input and all its parameters.
        It
            determines and set __step_input__
            determines and set pattern variables if needed
        returns
            _groups
            _vars
        which are groups of _input and related _vars
        """
        if ifiles.unspecified():
            env.sos_dict.set('step_input', sos_targets([]))
            env.sos_dict.set('_input', sos_targets([]))
            env.sos_dict.set('step_output', sos_targets())
            return [sos_targets([])], [{}]
        #
        if 'filetype' in kwargs:
            env.logger.warning('Input option filetype was deprecated')

        assert isinstance(ifiles, sos_targets)

        # input file is the filtered files
        env.sos_dict.set('step_input', ifiles)
        env.sos_dict.set('_input', ifiles)

        if ifiles._num_groups() == 0:
            ifiles._group('all')
        #
        return ifiles.groups

    def process_depends_args(self, dfiles: sos_targets, **kwargs):
        for k in kwargs.keys():
            if k not in SOS_DEPENDS_OPTIONS:
                raise RuntimeError(f'Unrecognized depends option {k}')
        if dfiles.undetermined():
            raise ValueError(r"Depends needs to handle undetermined")

        env.sos_dict.set('_depends', dfiles)
        if env.sos_dict['step_depends'] is None:
            env.sos_dict.set('step_depends', dfiles)
        # dependent files can overlap
        elif env.sos_dict['step_depends'] != dfiles:
            env.sos_dict['step_depends'].extend(dfiles)

    def process_output_args(self, ofiles: sos_targets, **kwargs):
        for k in kwargs.keys():
            if k not in SOS_OUTPUT_OPTIONS:
                raise RuntimeError(f'Unrecognized output option {k}')

        if ofiles._num_groups() > 0:
            if ofiles._num_groups() == 1:
                ofiles = ofiles._get_group(0)
            elif ofiles._num_groups() != len(self._substeps):
                raise RuntimeError(
                    f'Inconsistent number of output ({ofiles._num_groups()}) and input ({len(self._substeps)}) groups.')
            else:
                ofiles = ofiles._get_group(env.sos_dict['_index'])

        # create directory
        if ofiles.valid():
            for ofile in ofiles:
                if isinstance(ofile, file_target):
                    parent_dir = ofile.parent
                    if parent_dir and not parent_dir.is_dir():
                        parent_dir.mkdir(parents=True, exist_ok=True)

        # set variables
        env.sos_dict.set('_output', ofiles)
        #
        if not env.sos_dict['step_output'].valid():
            env.sos_dict.set('step_output', copy.deepcopy(ofiles))
        else:
            for ofile in ofiles:
                if ofile in env.sos_dict['step_output']._targets:
                    raise ValueError(
                        f'Output {ofile} from substep {env.sos_dict["_index"]} overlaps with output from a previous substep')
            env.sos_dict['step_output'].extend(ofiles, keep_groups=True)

    def process_task_args(self, **kwargs):
        env.sos_dict.set('_runtime', {})
        for k, v in kwargs.items():
            if k not in SOS_RUNTIME_OPTIONS:
                raise RuntimeError(f'Unrecognized runtime option {k}={v}')
            # standardize walltime to an integer
            if k == 'walltime':
                v = format_HHMMSS(v)
            elif k == 'mem':
                v = expand_size(v)
            env.sos_dict['_runtime'][k] = v

    def submit_task(self, task_id, taskdef, task_vars):
        if self.task_manager is None:
            if 'trunk_size' in env.sos_dict['_runtime']:
                if not isinstance(env.sos_dict['_runtime']['trunk_size'], int):
                    raise ValueError(
                        f'An integer value is expected for runtime option trunk, {env.sos_dict["_runtime"]["trunk_size"]} provided')
                trunk_size = env.sos_dict['_runtime']['trunk_size']
            else:
                trunk_size = 1
            if 'trunk_workers' in env.sos_dict['_runtime']:
                if not isinstance(env.sos_dict['_runtime']['trunk_workers'], int):
                    raise ValueError(
                        f'An integer value is expected for runtime option trunk_workers, {env.sos_dict["_runtime"]["trunk_workers"]} provided')
                trunk_workers = env.sos_dict['_runtime']['trunk_workers']
            else:
                trunk_workers = 0

            # if 'queue' in env.sos_dict['_runtime'] and env.sos_dict['_runtime']['queue']:
            #    host = env.sos_dict['_runtime']['queue']
            # else:
            #    # otherwise, use workflow default
            #    host = '__default__'

            self.task_manager = TaskManager(trunk_size, trunk_workers)

        # 618
        # it is possible that identical tasks are executed (with different underlying random numbers)
        # we should either give a warning or produce different ids...
        if self.task_manager.index_of(task_id) >= 0:
            raise RuntimeError(
                f'Task {task_id} generated for (_index={env.sos_dict["_index"]}) is identical to a previous one (_index={self.task_manager.index_of(task_id)}).')
        elif self.task_manager.has_output(task_vars['_output']):
            raise RuntimeError(
                f'Task produces output files {", ".join(task_vars["_output"])} that are output of other tasks.')
        # if no trunk_size, the job will be submitted immediately
        # otherwise tasks will be accumulated and submitted in batch
        self.task_manager.append(
            (task_id, taskdef, task_vars['_output']))
        tasks = self.task_manager.get_job()
        if tasks:
            self.submit_tasks(tasks)
        return task_id

    def wait_for_results(self, all_submitted):
        if self.concurrent_substep:
            self.wait_for_substep()

        if self.task_manager is None:
            return {}

        # submit the last batch of tasks
        tasks = self.task_manager.get_job(all_tasks=True)
        if tasks:
            self.submit_tasks(tasks)

        # waiting for results of specified IDs
        results = self.wait_for_tasks(self.task_manager._submitted_tasks, all_submitted)
        #
        # report task
        # what we should do here is to get the alias of the Host
        # because it can be different (e.g. not localhost
        if 'queue' in env.sos_dict['_runtime'] and env.sos_dict['_runtime']['queue']:
            queue = env.sos_dict['_runtime']['queue']
        elif env.config['default_queue']:
            queue = env.config['default_queue']
        else:
            queue = 'localhost'

        for id, result in results.items():
            # turn to string to avoid naming lookup issue
            rep_result = {x: (y if isinstance(y, (int, bool, float, str)) else short_repr(
                y)) for x, y in result.items()}
            rep_result['tags'] = ' '.join(self.task_manager.tags(id))
            rep_result['queue'] = queue
            env.signature_push_socket.send_pyobj(['workflow', 'task', id, repr(rep_result)])
        self.task_manager.clear_submitted()

        # if in dryrun mode, we display the output of the dryrun task
        if env.config['run_mode'] == 'dryrun':
            tid = list(results.keys())[0]
            tf = TaskFile(tid)
            if tf.has_stdout():
                print(TaskFile(tid).stdout)

        for idx, task in enumerate(self.proc_results):
            # if it is done
            if isinstance(task, dict):
                continue
            if task in results:
                self.proc_results[idx] = results[task]
            else:
                # can be a subtask
                for _, mres in results.items():
                    if 'subtasks' in mres and task in mres['subtasks']:
                        self.proc_results[idx] = mres['subtasks'][task]
                    elif 'exception' in mres:
                        self.proc_results[idx] = mres
        #
        # check if all have results?
        if any(isinstance(x, str) for x in self.proc_results):
            raise RuntimeError(
                f'Failed to get results for tasks {", ".join(x for x in self.proc_results if isinstance(x, str))}')
        #
        for idx, res in enumerate(self.proc_results):
            if 'skipped' in res and res['skipped']:
                self.completed['__task_skipped__'] += 1
                # complete case: task skipped
                env.controller_push_socket.send_pyobj(['progress', 'substep_completed', env.sos_dict['step_id']])
            else:
                # complete case: task completed
                env.controller_push_socket.send_pyobj(['progress', 'substep_ignored', env.sos_dict['step_id']])
                self.completed['__task_completed__'] += 1
            if 'shared' in res:
                self.shared_vars[idx].update(res['shared'])

    def log(self, stage=None, msg=None):
        if stage == 'start':
            env.logger.info(
                f'{"Checking" if env.config["run_mode"] == "dryrun" else "Running"} ``{self.step.step_name(True)}``: {self.step.comment.strip()}')
        elif stage == 'input statement':
            env.logger.trace(f'Handling input statement {msg}')
        elif stage == '_input':
            if env.sos_dict['_input'] is not None:
                env.logger.debug(
                    f'_input: ``{short_repr(env.sos_dict["_input"])}``')
        elif stage == '_depends':
            if env.sos_dict['_depends'] is not None:
                env.logger.debug(
                    f'_depends: ``{short_repr(env.sos_dict["_depends"])}``')
        elif stage == 'input':
            if env.sos_dict['step_input'] is not None:
                env.logger.info(
                    f'input:   ``{short_repr(env.sos_dict["step_input"])}``')
        elif stage == 'output':
            if env.sos_dict['step_output'] is not None and len(env.sos_dict['step_output']) > 0:
                env.logger.info(
                    f'output:   ``{short_repr(env.sos_dict["step_output"])}``')

    def execute(self, stmt):
        try:
            self.last_res = SoS_exec(
                stmt, return_result=self.run_mode == 'interactive')
        except (StopInputGroup, TerminateExecution, UnknownTarget, RemovedTarget, UnavailableLock):
            raise
        except subprocess.CalledProcessError as e:
            raise RuntimeError(e.stderr)
        except ArgumentError:
            raise
        except Exception as e:
            raise RuntimeError(get_traceback_msg(e))

    def prepare_substep(self):
        # socket to collect result
        self.result_pull_socket = env.zmq_context.socket(zmq.PULL)
        port = self.result_pull_socket.bind_to_random_port('tcp://127.0.0.1')
        env.config['sockets']['result_push_socket'] = port

    def submit_substep(self, substep):
        env.substep_frontend_socket.send_pyobj(substep)

    def wait_for_substep(self):
        for ss in self.proc_results:
            res = self.result_pull_socket.recv_pyobj()
            #
            if "index" not in res:
                raise RuntimeError("Result received from substep does not have key index")
            if 'task_id' in res:
                # if substep returns tasks, ...
                task = self.submit_task(res['task_id'], res['task_def'], res['task_vars'])
                self.proc_results[res['index']] = task
            else:
                self.proc_results[res['index']] = res

    def collect_result(self):
        # only results will be sent back to the master process
        #
        # __step_input__:    input of this step
        # __steo_output__:   output of this step
        # __step_depends__:  dependent files of this step

        result = {
            '__step_input__': env.sos_dict['step_input'],
            '__step_output__': env.sos_dict['step_output'],
            '__step_depends__': env.sos_dict['step_depends'],
            '__step_name__': env.sos_dict['step_name'],
            '__completed__': self.completed,
        }
        result['__last_res__'] = self.last_res
        result['__shared__'] = {}
        if 'shared' in self.step.options:
            result['__shared__'] = self.shared_vars
        env.controller_push_socket.send_pyobj(['progress', 'step_completed',
            -1 if 'sos_run' in env.sos_dict['__signature_vars__'] else self.completed['__step_completed__'],
            env.sos_dict['step_name'], env.sos_dict['step_output']])
        return result

    def run(self):
        '''Execute a single step and return results. The result for batch mode is the
        input, output etc returned as alias, and for interactive mode is the return value
        of the last expression. '''
        # return value of the last executed statement
        self.last_res = None
        self.start_time = time.time()
        self.completed = defaultdict(int)
        #
        # prepare environments, namely variables that can be used by the step
        #
        # * step_name:  name of the step, can be used by step process to determine
        #               actions dynamically.
        env.sos_dict.set('step_name', self.step.step_name())
        self.log('start')
        env.sos_dict.set('step_id', hash((env.sos_dict["workflow_id"], env.sos_dict["step_name"], self.step.md5)))
        # used by nested workflow
        env.sos_dict.set('__step_context__', self.step.context)

        env.sos_dict.set('_runtime', {})
        # * input:      input files, which should be __step_output__ if it is defined, or
        #               None otherwise.
        # * _input:     first batch of input, which should be input if no input statement is used
        # * output:     None at first, can be redefined by output statement
        # * _output:    None at first, can be redefined by output statement
        # * depends:    None at first, can be redefined by depends statement
        # * _depends:   None at first, can be redefined by depends statement
        #
        if '__step_output__' not in env.sos_dict or env.sos_dict['__step_output__'].unspecified():
            env.sos_dict.set('step_input', sos_targets([]))
        else:
            env.sos_dict.set('step_input', env.sos_dict['__step_output__'])
        # input can be Undetermined from undetermined output from last step
        env.sos_dict.set('_input', copy.deepcopy(env.sos_dict['step_input']))

        if '__default_output__' in env.sos_dict:
            # if step is triggered by sos_step, it should not be considered as
            # output of the step. #981
            env.sos_dict.set('__default_output__', sos_targets(
                [x for x in env.sos_dict['__default_output__']._targets
                 if not isinstance(x, sos_step)]))
            env.sos_dict.set('step_output', copy.deepcopy(
                env.sos_dict['__default_output__']))
            env.sos_dict.set('_output', copy.deepcopy(
                env.sos_dict['__default_output__']))
        else:
            env.sos_dict.set('step_output', sos_targets([]))
            # output is said to be unspecified until output: is used
            env.sos_dict.set('_output', sos_targets(_undetermined=True))

        env.sos_dict.set('step_depends', sos_targets([]))
        env.sos_dict.set('_depends', sos_targets([]))
        # _index is needed for pre-input action's active option and for debug output of scripts
        env.sos_dict.set('_index', 0)

        env.logger.trace(
            f'Executing step {env.sos_dict["step_name"]} with step_input {env.sos_dict["step_input"]} and step_output {env.sos_dict["step_output"]}')

        task_statement = [x[2] for x in enumerate(
            self.step.statements) if x[0] == ':' and x[1] == 'task']
        if task_statement:
            try:
                val = get_value_of_param('queue', task_statement, extra_dict=env.sos_dict._dict)
                if val:
                    env.sos_dict['_runtime']['queue'] = val[0]
            except Exception as e:
                raise ValueError(f'Failed to determine value of parameter queue of {task_statement}: {e}')
        if (env.config['default_queue'] in ('None', 'none', None) and
            'queue' not in env.sos_dict['_runtime']) or \
            ('queue' in env.sos_dict['_runtime'] and
            env.sos_dict['_runtime']['default_queue'] in ('none', 'None', None)):
            # remove task statement
            if len(self.step.statements) >= 1 and self.step.statements[-1][0] == '!':
                self.step.statements[-1][1].append('\n' + self.step.task)
            else:
                self.step.statements.append(
                    ['!', self.step.task]
                )
            self.step.task = None


        # look for input statement.
        input_statement_idx = [idx for idx, x in enumerate(
            self.step.statements) if x[0] == ':' and x[1] == 'input']
        if not input_statement_idx:
            input_statement_idx = None
        elif len(input_statement_idx) == 1:
            input_statement_idx = input_statement_idx[0]
        else:
            raise ValueError(
                f'More than one step input are specified in step {self.step.step_name()}')

        # if shared is true, we have to disable concurrent because we
        # do not yet return anything from shared.
        self.concurrent_substep = 'shared' not in self.step.options
        if input_statement_idx is not None:
            # execute before input stuff
            for statement in self.step.statements[:input_statement_idx]:
                if statement[0] == ':':
                    key, value = statement[1:3]
                    if key != 'depends':
                        raise ValueError(
                            f'Step input should be specified before {key}')
                    try:
                        args, kwargs = SoS_eval(f'__null_func__({value})',
                            extra_dict={
                                '__null_func__': __null_func__,
                                'output_from': __output_from__,
                                'named_output': __named_output__
                                }
                            )
                        dfiles = expand_depends_files(*args)
                        # dfiles can be Undetermined
                        self.process_depends_args(dfiles, **kwargs)
                    except (UnknownTarget, RemovedTarget, UnavailableLock):
                        raise
                    except Exception as e:
                        raise RuntimeError(
                            f'Failed to process step {key} ({value.strip()}): {e}')
                else:
                    try:
                        self.execute(statement[1])
                    except StopInputGroup as e:
                        if e.message:
                            env.logger.warning(e)
                        return self.collect_result()
            # input statement
            stmt = self.step.statements[input_statement_idx][2]
            self.log('input statement', stmt)
            try:
                args, kwargs = SoS_eval(f"__null_func__({stmt})",
                            extra_dict={
                                '__null_func__': __null_func__,
                                'output_from': __output_from__,
                                'named_output': __named_output__
                                }
                )
                # Files will be expanded differently with different running modes
                input_files: sos_targets = expand_input_files(stmt, *args,
                    **{k:v for k,v in kwargs.items() if k not in SOS_INPUT_OPTIONS})
                self._substeps = self.process_input_args(
                    input_files, **{k:v for k,v in kwargs.items() if k in SOS_INPUT_OPTIONS})
                #
                if 'concurrent' in kwargs and kwargs['concurrent'] is False:
                    self.concurrent_substep = False
            except (UnknownTarget, RemovedTarget, UnavailableLock):
                raise
            except Exception as e:
                raise ValueError(
                    f'Failed to process input statement {stmt}: {e}')

            input_statement_idx += 1
        elif env.sos_dict['step_input'].groups:
            # if default has groups...
            # default case
            self._substeps = env.sos_dict['step_input'].groups
            # assuming everything starts from 0 is after input
            input_statement_idx = 0
        else:
            # default case
            self._substeps = [env.sos_dict['step_input']]
            # assuming everything starts from 0 is after input
            input_statement_idx = 0

        self.proc_results = []
        self.vars_to_be_shared = set()
        if 'shared' in self.step.options:
            self.vars_to_be_shared = parse_shared_vars(self.step.options['shared'])
        self.vars_to_be_shared = sorted([x[5:] if x.startswith('step_') else x for x in self.vars_to_be_shared if x not in ('step_', 'step_input', 'step_output', 'step_depends')])
        self.shared_vars = [{} for x in self._substeps]
        # run steps after input statement, which will be run multiple times for each input
        # group.
        env.sos_dict.set('__num_groups__', len(self._substeps))

        # determine if a single index or the whole step should be skipped
        skip_index = False
        # signatures of each index, which can remain to be None if no output
        # is defined.
        self.output_groups = [[] for x in self._substeps]

        if self.concurrent_substep:
            if len(self._substeps) <= 1 or self.run_mode == 'dryrun':
                self.concurrent_substep = False
            if len([
                    x for x in self.step.statements[input_statement_idx:] if x[0] != ':']) > 1:
                self.concurrent_substep = False
                env.logger.debug(
                    'Input groups are executed sequentially because of existence of directives between statements.')
            elif any('sos_run' in x[1] for x in self.step.statements[input_statement_idx:]):
                self.concurrent_substep = False
                env.logger.debug(
                    'Input groups are executed sequentially because of existence of nested workflow.')
            else:
                self.prepare_substep()

        try:
            self.completed['__substep_skipped__'] = 0
            self.completed['__substep_completed__'] = len(self._substeps)
            # pending signatures are signatures for steps with external tasks
            pending_signatures = [None for x in self._substeps]
            for idx, g in enumerate(self._substeps):
                # other variables
                #
                _vars = {}
                # now, let us expose target level variables as lists
                if len(g) > 1:
                    names = set.union(*[set(x._dict.keys()) for x in g._targets])
                elif len(g) == 1:
                    names = set(g._targets[0]._dict.keys())
                else:
                    names = set()
                for name in names:
                    _vars[name] = [x.get(name) for x in g._targets]
                # then we expose all group level variables
                _vars.update(g._dict)
                _vars.update(env.sos_dict['step_input']._dict)
                env.sos_dict.update(_vars)

                env.sos_dict.set('_input', copy.deepcopy(g))
                # set vars to _input
                #env.sos_dict['_input'].set(**v)

                self.log('_input')
                env.sos_dict.set('_index', idx)

                # in interactive mode, because sos_dict are always shared
                # execution of a substep, especially when it calls a nested
                # workflow, would change step_name, __step_context__ etc, and
                # we will have to reset these variables to make sure the next
                # substep would execute normally. Batch mode is immune to this
                # problem because nested workflows are executed in their own
                # process/context etc
                if env.config['run_mode'] == 'interactive':
                    env.sos_dict.set('step_name', self.step.step_name())
                    env.sos_dict.set('step_id', hash((env.sos_dict["workflow_id"], env.sos_dict["step_name"], self.step.md5)))
                    # used by nested workflow
                    env.sos_dict.set('__step_context__', self.step.context)
                #
                pre_statement = []
                if not any(st[0] == ':' and st[1] == 'output' for st in self.step.statements[input_statement_idx:]) and \
                        '__default_output__' in env.sos_dict:
                    pre_statement = [[':', 'output', '_output']]

                # if there is no statement, no task, claim success
                post_statement = []
                if not any(st[0] == '!' for st in self.step.statements[input_statement_idx:]):
                    if self.step.task:
                        # if there is only task, we insert a fake statement so that it can be executed by the executor
                        post_statement = [['!', '']]
                    else:
                        # complete case: no step, no statement
                        env.controller_push_socket.send_pyobj(['progress', 'substep_completed', env.sos_dict['step_id']])

                for statement in pre_statement + self.step.statements[input_statement_idx:] + post_statement:
                    # if input is undertermined, we can only process output:
                    if not g.valid() and statement[0] != ':':
                        raise RuntimeError('Undetermined input encountered')
                        return self.collect_result()
                    if statement[0] == ':':
                        key, value = statement[1:3]
                        # output, depends, and process can be processed multiple times
                        try:
                            args, kwargs = SoS_eval(f'__null_func__({value})',
                                extra_dict={
                                    '__null_func__': __null_func__,
                                    'output_from': __output_from__,
                                    'named_output': __named_output__
                                    })
                            # dynamic output or dependent files
                            if key == 'output':
                                # if output is defined, its default value needs to be cleared
                                if idx == 0:
                                    env.sos_dict.set(
                                        'step_output', sos_targets())
                                ofiles: sos_targets = expand_output_files(value, *args,
                                    **{k:v for k,v in kwargs.items() if k not in SOS_OUTPUT_OPTIONS})
                                if g.valid() and ofiles.valid():
                                    if any(x in g._targets for x in ofiles if not isinstance(x, sos_step)):
                                        raise RuntimeError(
                                            f'Overlapping input and output files: {", ".join(repr(x) for x in ofiles if x in g)}')

                                # set variable _output and output
                                self.process_output_args(ofiles, **{k:v for k,v in kwargs.items() if k in SOS_OUTPUT_OPTIONS})
                                self.output_groups[idx] = env.sos_dict['_output']
                            elif key == 'depends':
                                try:
                                    dfiles = expand_depends_files(*args)
                                    # dfiles can be Undetermined
                                    self.process_depends_args(dfiles, **kwargs)
                                    self.log('_depends')
                                except Exception as e:
                                    env.logger.info(e)
                                    raise
                            elif key == 'task':
                                # we process task options a the beginning of the
                                # step in case it users specify -q none, but need
                                # to do it again because the options might involve
                                # _output #1129
                                process_task_args(**kwargs)
                            else:
                                raise RuntimeError(
                                    f'Unrecognized directive {key}')
                        except (UnknownTarget, RemovedTarget, UnavailableLock):
                            raise
                        except Exception as e:
                            # if input is Undertermined, it is possible that output cannot be processed
                            # due to that, and we just return
                            if not g.valid():
                                env.logger.debug(e)
                                return self.collect_result()
                            raise RuntimeError(
                                f'Failed to process step {key} ({value.strip()}): {e}')
                    else:
                        try:
                            if self.concurrent_substep:
                                env.logger.trace(f'Execute substep {env.sos_dict["step_name"]} concurrently')

                                #
                                # step_output: needed only when it is undetermined
                                # step_input: not needed
                                # _input, _output, _depends, _index: needed
                                # __args__ for the processing of parameters
                                # step_name: for debug scripts
                                # step_id, workflow_id: for reporting to controller
                                # '__signature_vars__' to be used for signature creation
                                #
                                # __step_context__ is not needed because substep
                                # executor does not support nested workflow
                                proc_vars = env.sos_dict.clone_selected_vars(
                                    env.sos_dict['__signature_vars__']
                                    | {'_input', '_output', '_depends', '_index',
                                     'step_output', '__args__', 'step_name',
                                      '_runtime', 'step_id', 'workflow_id', '__num_groups__',
                                      '__signature_vars__'})

                                self.proc_results.append({})
                                self.submit_substep(dict(stmt=statement[1],
                                    global_def=self.step.global_def,
                                    task=self.step.task,
                                    proc_vars=proc_vars,
                                    shared_vars=self.vars_to_be_shared,
                                    config=env.config))
                            else:
                                if env.config['sig_mode'] == 'ignore' or env.sos_dict['_output'].unspecified():
                                    env.logger.trace(f'Execute substep {env.sos_dict["step_name"]} without signature')
                                    try:
                                        verify_input()
                                        self.execute(statement[1])
                                    finally:
                                        if not self.step.task:
                                            # if no task, this step is __completed
                                            # complete case: local skip without task
                                            env.controller_push_socket.send_pyobj(['progress', 'substep_completed', env.sos_dict['step_id']])
                                    if 'shared' in self.step.options:
                                        try:
                                            self.shared_vars[env.sos_dict['_index']].update({
                                                x:env.sos_dict[x] for x in self.vars_to_be_shared
                                                    if x in env.sos_dict})
                                        except Exception as e:
                                            raise ValueError(f'Missing shared variable {e}.')
                                else:
                                    sig = RuntimeInfo(
                                        statementMD5([statement[1], self.step.task]),
                                        env.sos_dict['_input'],
                                        env.sos_dict['_output'],
                                        env.sos_dict['_depends'],
                                        env.sos_dict['__signature_vars__'],
                                        shared_vars=self.vars_to_be_shared)
                                    env.logger.trace(f'Execute substep {env.sos_dict["step_name"]} with signature {sig.sig_id}')
                                    # if singaure match, we skip the substep even  if
                                    # there are tasks.
                                    matched = validate_step_sig(sig)
                                    skip_index = bool(matched)
                                    if matched:
                                        if env.sos_dict['step_output'].undetermined():
                                            self.output_groups[env.sos_dict['_index']] = matched["output"]
                                        if 'vars' in matched:
                                            self.shared_vars[env.sos_dict['_index']].update(matched["vars"])
                                        # complete case: local skip without task
                                        env.controller_push_socket.send_pyobj(['progress', 'substep_ignored', env.sos_dict['step_id']])
                                    else:
                                        sig.lock()
                                        try:
                                            verify_input()
                                            self.execute(statement[1])
                                            if 'shared' in self.step.options:
                                                try:
                                                    self.shared_vars[env.sos_dict['_index']].update({
                                                        x:env.sos_dict[x] for x in self.vars_to_be_shared
                                                            if x in env.sos_dict})
                                                except Exception as e:
                                                    raise ValueError(f'Missing shared variable {e}.')
                                        finally:
                                            # if this is the end of substep, save the signature
                                            # otherwise we need to wait for the completion
                                            # of the task.
                                            if not self.step.task:
                                                if env.sos_dict['step_output'].undetermined():
                                                    output = reevaluate_output()
                                                    self.output_groups[env.sos_dict['_index']] = output
                                                    sig.set_output(output)
                                                sig.write()
                                                # complete case : local execution without task
                                                env.controller_push_socket.send_pyobj(['progress', 'substep_completed', env.sos_dict['step_id']])
                                            else:
                                                pending_signatures[idx] = sig
                                            sig.release()


                        except StopInputGroup as e:
                            self.output_groups[idx] = []
                            if e.message:
                                env.logger.info(e)
                            skip_index = True
                            break
                        except Exception as e:
                            clear_output(e)
                            raise

                # if there is no statement , but there are tasks, we should
                # check signature here.
                if not any(x[0] == '!' for x in self.step.statements[input_statement_idx:]) and self.step.task and not self.concurrent_substep \
                    and env.config['sig_mode'] != 'ignore' and not env.sos_dict['_output'].unspecified():
                    sig = RuntimeInfo(
                        statementMD5([self.step.task]),
                        env.sos_dict['_input'],
                        env.sos_dict['_output'],
                        env.sos_dict['_depends'],
                        env.sos_dict['__signature_vars__'],
                        shared_vars=self.vars_to_be_shared)
                    env.logger.trace(f'Check task-only step {env.sos_dict["step_name"]} with signature {sig.sig_id}')
                    matched = validate_step_sig(sig)
                    skip_index = bool(matched)
                    if matched:
                        if env.sos_dict['step_output'].undetermined():
                            self.output_groups[env.sos_dict['_index']] = matched["output"]
                        self.shared_vars[env.sos_dict['_index']].update(matched["vars"])
                        # complete case: step with task ignored
                        env.controller_push_socket.send_pyobj(['progress', 'substep_ignored', env.sos_dict['step_id']])
                    pending_signatures[idx] = sig

                # if this index is skipped, go directly to the next one
                if skip_index:
                    self.completed['__substep_skipped__'] += 1
                    self.completed['__substep_completed__'] -= 1
                    skip_index = False
                    continue

                # if concurrent input group, tasks are handled in substep
                if self.concurrent_substep or not self.step.task:
                    continue

                if env.config['run_mode'] == 'dryrun' and env.sos_dict['_index'] != 0:
                    continue

                # check if the task is active
                if 'active' in env.sos_dict['_runtime']:
                    active = env.sos_dict['_runtime']['active']
                    if active is True:
                        pass
                    elif active is False:
                        continue
                    elif isinstance(active, int):
                        if active >= 0 and env.sos_dict['_index'] != active:
                            continue
                        if active < 0 and env.sos_dict['_index'] != active + env.sos_dict['__num_groups__']:
                            continue
                    elif isinstance(active, Sequence):
                        allowed_index = list(
                            [x if x >= 0 else env.sos_dict['__num_groups__'] + x for x in active])
                        if env.sos_dict['_index'] not in allowed_index:
                            continue
                    elif isinstance(active, slice):
                        allowed_index = list(
                            range(env.sos_dict['__num_groups__']))[active]
                        if env.sos_dict['_index'] not in allowed_index:
                            continue
                    else:
                        raise RuntimeError(
                            f'Unacceptable value for option active: {active}')

                #
                self.log('task')
                try:
                    task_id, taskdef, task_vars = create_task(self.step.global_def, self.step.task)
                    task = self.submit_task(task_id, taskdef, task_vars)
                    self.proc_results.append(task)
                except Exception as e:
                    # FIXME: cannot catch exception from subprocesses
                    if env.verbosity > 2:
                        sys.stderr.write(get_traceback())
                    raise RuntimeError(
                        f'Failed to execute process\n"{short_repr(self.step.task)}"\n{e}')
                #
                # if not concurrent, we have to wait for the completion of the task
                if 'concurrent' in env.sos_dict['_runtime'] and env.sos_dict['_runtime']['concurrent'] is False:
                    self.wait_for_results(all_submitted=False)
                #
                # endfor loop for each input group
                #
            self.wait_for_results(all_submitted=True)
            for idx, res in enumerate(self.proc_results):
                if 'sig_skipped' in res:
                    self.completed['__substep_skipped__'] += 1
                    self.completed['__substep_completed__'] -= 1
                if 'output' in res and env.sos_dict['step_output'].undetermined():
                    self.output_groups[idx] = res['output']
            # check results
            for proc_result in [x for x in self.proc_results if x['ret_code'] == 0]:
                if 'stdout' in proc_result and proc_result['stdout']:
                    sys.stdout.write(proc_result['stdout'])
                if 'stderr' in proc_result and proc_result['stderr']:
                    sys.stderr.write(proc_result['stderr'])

            for proc_result in [x for x in self.proc_results if x['ret_code'] != 0]:
                if 'stdout' in proc_result and proc_result['stdout']:
                    sys.stdout.write(proc_result['stdout'])
                if 'stderr' in proc_result and proc_result['stderr']:
                    sys.stderr.write(proc_result['stderr'])
                if 'exception' in proc_result:
                    e = proc_result['exception']
                    if isinstance(e, StopInputGroup):
                        if e.message:
                            env.logger.info(e)
                        self.output_groups[proc_result['index']] = []
                    else:
                        raise e
            # if output is Undetermined, re-evalulate it
            # finalize output from output_groups because some output might be skipped
            # this is the final version of the output but we do maintain output
            # during the execution of step, for compatibility.
            env.sos_dict.set('step_output', sos_targets([]))

            for og in self.output_groups:
                og = sos_targets(og)
                env.sos_dict['step_output']._add_group(sos_targets(og))

            # now that output is settled, we can write remaining signatures
            for idx, res in enumerate(self.proc_results):
                if pending_signatures[idx] is not None:
                    if res['ret_code'] == 0:
                        pending_signatures[idx].write()

            # if there exists an option shared, the variable would be treated as
            # provides=sos_variable(), and then as step_output
            if 'shared' in self.step.options:
                self.shared_vars = evaluate_shared(self.shared_vars, self.step.options['shared'])
                env.sos_dict.quick_update(self.shared_vars)
            self.log('output')
            self.verify_output()
            substeps = self.completed['__substep_completed__'] + \
                self.completed['__substep_skipped__']
            self.completed['__step_completed__'] = self.completed['__substep_completed__'] / substeps
            self.completed['__step_skipped__'] = self.completed['__substep_skipped__'] / substeps
            if self.completed['__step_completed__'].is_integer():
                self.completed['__step_completed__'] = int(
                    self.completed['__step_completed__'])
            if self.completed['__step_skipped__'].is_integer():
                self.completed['__step_skipped__'] = int(
                    self.completed['__step_skipped__'])

            def file_only(targets):
                if not isinstance(targets, sos_targets):
                    env.logger.warning(
                        f"Unexpected input or output target for reporting. Empty list returned: {targets}")
                    return []
                else:
                    return [(str(x), x.size()) for x in targets._targets if isinstance(x, file_target)]
            step_info = {
                'step_id': self.step.md5,
                'start_time': self.start_time,
                'stepname': self.step.step_name(True),
                'substeps': len(self._substeps),
                'input': file_only(env.sos_dict['step_input']),
                'output': file_only(env.sos_dict['step_output']),
                'completed': dict(self.completed),
                'end_time': time.time()
            }
            env.signature_push_socket.send_pyobj([
                'workflow', 'step', env.sos_dict["workflow_id"], repr(step_info)])
            return self.collect_result()
        finally:
            if self.concurrent_substep:
                self.result_pull_socket.close()



class Step_Executor(Base_Step_Executor):
    '''Single process step executor'''

    def __init__(self, step, socket, mode='run'):
        self.run_mode = mode
        env.config['run_mode'] = mode
        super(Step_Executor, self).__init__(step)
        self.socket = socket
        # because step is executed in a separate SoS_Worker process, this
        # __socket__ is available to all the actions that will be executed
        # in the step
        env.__socket__ = socket


    def submit_tasks(self, tasks):
        env.logger.debug(f'Send {tasks}')
        if 'queue' in env.sos_dict['_runtime'] and env.sos_dict['_runtime']['queue']:
            host = env.sos_dict['_runtime']['queue']
        else:
            # otherwise, use workflow default
            host = '__default__'
        self.socket.send_pyobj(f'tasks {host} {" ".join(tasks)}')

    def wait_for_tasks(self, tasks, all_submitted):
        if not tasks:
            return {}
        # when we wait, the "outsiders" also need to see the tags etc
        # of the tasks so we have to write to the database. #156
        env.signature_push_socket.send_pyobj(['commit'])
        # wait till the executor responde
        results = {}
        while True:
            res = self.socket.recv_pyobj()
            if res is None:
                sys.exit(0)
            results.update(res)

            # all results have been obtained.
            if len(results) == len(tasks):
                break
        return results

    def run(self):
        try:
            res = Base_Step_Executor.run(self)
            if self.socket is not None:
                env.logger.debug(
                    f'Step {self.step.step_name()} sends result {short_repr(res)}')
                self.socket.send_pyobj(res)
            else:
                return res
        except Exception as e:
            clear_output(e)
            if env.verbosity > 2:
                sys.stderr.write(get_traceback())
            if self.socket is not None:
                env.logger.debug(
                    f'Step {self.step.step_name()} sends exception {e}')
                self.socket.send_pyobj(e)
            else:
                raise e
