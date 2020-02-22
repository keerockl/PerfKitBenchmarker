# Copyright 2019 PerfKitBenchmarker Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Run NCCL benchmarks."""

import collections
import re
import time
import numpy as np
from perfkitbenchmarker import configs
from perfkitbenchmarker import flags
from perfkitbenchmarker import regex_util
from perfkitbenchmarker import sample
from perfkitbenchmarker import vm_util

flags.DEFINE_integer('nccl_slots', 8,
                     'Launch n processes per node on all allocated nodes')
flags.DEFINE_string('nccl_cuda_visible_devices', None, 'GPU identifiers are '
                    'given as integer indices or as UUID strings.')
flags.DEFINE_list('nccl_extra_params', [], 'Export an environment variable')
flags.DEFINE_string('nccl_minbytes', '8', 'Minimum size to start with')
flags.DEFINE_string('nccl_maxbytes', '256M', 'Maximum size to start with')
flags.DEFINE_integer('nccl_stepfactor', 2,
                     'Multiplication factor between sizes')
flags.DEFINE_integer('nccl_ngpus', 1, 'Number of gpus per thread.')
flags.DEFINE_boolean('nccl_check', False, 'Check correctness of results.')
flags.DEFINE_integer('nccl_nthreads', 1, 'Number of threads per process')
flags.DEFINE_integer('nccl_num_runs', 10, 'The number of consecutive run.',
                     lower_bound=1)
flags.DEFINE_integer('nccl_seconds_between_runs', 10,
                     'Sleep between consecutive run.')
flags.DEFINE_boolean('nccl_install_openmpi', False, 'Install Open MPI')
flags.DEFINE_boolean('nccl_install_nccl', False, 'Install NCCL')
flags.DEFINE_integer('nccl_iters', 20, 'Number of iterations')
flags.DEFINE_string('nccl_mpi', '/usr/bin/mpirun', 'MPI binary path')
flags.DEFINE_string('nccl_mpi_home', '/usr/lib/x86_64-linux-gnu/openmpi',
                    'MPI home')
flags.DEFINE_string('nccl_nccl_home', '/usr/local/nccl2', 'NCCL home')
flags.DEFINE_boolean('nccl_install_mofed', False,
                     'Install Mellanox OpenFabrics drivers')


FLAGS = flags.FLAGS

BENCHMARK_NAME = 'nccl'
BENCHMARK_CONFIG = """
nccl:
  description: Runs NCCL Benchmark. Specify the number of VMs with --num_vms.
  vm_groups:
    default:
      vm_count: null
      vm_spec:
        GCP:
          machine_type: n1-highmem-96
          zone: us-central1-a
          image_family: tf-latest-gpu-gvnic
          image_project: deeplearning-platform-release
          boot_disk_size: 100
          gpu_type: v100
          gpu_count: 8
        AWS:
          machine_type: p3dn.24xlarge
          zone: us-west-2a
          image: ami-07728e9e2742b0662
          boot_disk_size: 100
        Azure:
          machine_type: Standard_NC24rs_v3
          zone: eastus
"""

_HOSTFILE = 'HOSTFILE'
_SAMPLE_LINE_RE = re.compile(r'# nThread (?P<nThread>\d+) '
                             r'nGpus (?P<nGpus>\d+) '
                             r'minBytes (?P<minBytes>\d+) '
                             r'maxBytes (?P<maxBytes>\d+) '
                             r'step: (?P<step>\S+) '
                             r'warmup iters: (?P<warmup_iters>\d+) '
                             r'iters: (?P<iters>\d+) '
                             r'validation: (?P<validation>\d+)\s*')

# Without '--mca btl_tcp_if_exclude docker0,lo', it stuck forever
# This is caused by Docker network in DLVM, use mca btl_tcp_if_exclude to skip
# docker network.

_RUN_CMD = ('{mpi} '
            '--hostfile {hostfile} '
            '--mca btl tcp,self '
            '--mca btl_tcp_if_exclude docker0,lo '
            '--bind-to none '
            '-N {slots} '
            '{env} '
            'nccl-tests/build/all_reduce_perf '
            '--minbytes {minbytes} '
            '--maxbytes {maxbytes} '
            '--stepfactor {stepfactor} '
            '--ngpus {ngpus} '
            '--check {check} '
            '--nthreads {nthreads} '
            '--iters {iters}')

_DEFAULT = 'DEFAULT'

_METADATA_COLUMNS = ('size', 'count', 'nccl_type', 'redop', 'out_of_place_time',
                     'out_of_place_algbw', 'out_of_place_busbw',
                     'out_of_place_error', 'in_place_time', 'in_place_algbw',
                     'in_place_busbw', 'in_place_error')

_SAMPLE_NAMES = {'Out of place algorithm bandwidth': 'out_of_place_algbw',
                 'Out of place bus bandwidth': 'out_of_place_busbw',
                 'In place algorithm bandwidth': 'in_place_algbw',
                 'In place bus bandwidth': 'in_place_busbw'}


def GetConfig(user_config):
  """Load and return benchmark config.

  Args:
    user_config: user supplied configuration (flags and config file)

  Returns:
    loaded benchmark configuration
  """
  return configs.LoadConfig(BENCHMARK_CONFIG, user_config, BENCHMARK_NAME)


def _PrepareVm(vm):
  """Install and set up NCCL on the target vm.

  Args:
    vm: virtual machine on which to install NCCL
  """
  vm.AuthenticateVm()
  vm.Install('cuda_toolkit')
  if FLAGS.nccl_install_nccl:
    vm.Install('nccl')
  if FLAGS.nccl_install_openmpi:
    vm.Install('openmpi')

  env = ''
  if FLAGS.aws_efa:
    env = ('export LD_LIBRARY_PATH=/opt/amazon/efa/lib:/opt/amazon/efa/lib64:'
           '$LD_LIBRARY_PATH &&')
    vm.InstallPackages('libudev-dev libtool autoconf')
    vm.RemoteCommand('git clone https://github.com/aws/aws-ofi-nccl.git -b aws')
    vm.RemoteCommand('cd aws-ofi-nccl && ./autogen.sh && ./configure '
                     '--with-mpi={mpi} '
                     '--with-libfabric=/opt/amazon/efa '
                     '--with-nccl={nccl} '
                     '--with-cuda={cuda} && sudo make && '
                     'sudo make install'.format(
                         mpi=FLAGS.nccl_mpi_home,
                         nccl=FLAGS.nccl_nccl_home,
                         cuda='/usr/local/cuda-{}'.format(
                             FLAGS.cuda_toolkit_version)))
  if FLAGS.nccl_install_mofed:
    vm.Install('mofed')
  vm.RemoteCommand('rm -rf nccl-tests')
  vm.RemoteCommand('git clone https://github.com/NVIDIA/nccl-tests.git')
  vm.RemoteCommand('cd nccl-tests && {env} make MPI=1 MPI_HOME={mpi} '
                   'NCCL_HOME={nccl} CUDA_HOME={cuda}'.format(
                       env=env, mpi=FLAGS.nccl_mpi_home,
                       nccl=FLAGS.nccl_nccl_home,
                       cuda='/usr/local/cuda-{}'.format(
                           FLAGS.cuda_toolkit_version)))


def Prepare(benchmark_spec):
  """Install and set up NCCL on the target vm.

  Args:
    benchmark_spec: The benchmark specification
  """
  benchmark_spec.always_call_cleanup = True
  vm_util.RunThreaded(_PrepareVm, benchmark_spec.vms)
  host = benchmark_spec.vms[0]
  host.RemoteCommand('rm -rf {hostfile}'.format(hostfile=_HOSTFILE))
  for vm in benchmark_spec.vms:
    cmd = 'echo "{ip} slots={slots}" >> {hostfile}'.format(
        ip=vm.internal_ip, hostfile=_HOSTFILE, slots=FLAGS.nccl_slots)
    host.RemoteCommand(cmd)


def _CreateMetadataDict():
  """Create metadata dict to be used in run results.

  Returns:
    metadata dict
  """
  metadata = {'slots': FLAGS.nccl_slots,
              'minbytes': FLAGS.nccl_minbytes,
              'maxbytes': FLAGS.nccl_maxbytes,
              'stepfactor': FLAGS.nccl_stepfactor,
              'ngpus': FLAGS.nccl_ngpus,
              'check': FLAGS.nccl_check,
              'nthreads': FLAGS.nccl_nthreads,
              'iters': FLAGS.nccl_iters,
              'cuda_visible_devices': FLAGS.nccl_cuda_visible_devices}
  if FLAGS.nccl_install_mofed:
    metadata['mofed_version'] = FLAGS.mofed_version
  return metadata


def MakeSamplesFromOutput(metadata, output):
  """Create samples containing metrics.

  Args:
    metadata: dict contains all the metadata that reports.
    output: string, command output
  Example output:
    perfkitbenchmarker/tests/linux_benchmarks/nccl_benchmark_test.py

  Returns:
    Samples containing training metrics, and the bandwidth
  """
  samples = []
  metadata.update(_SAMPLE_LINE_RE.match(output).groupdict())
  results = regex_util.ExtractAllMatches(
      r'(Rank\s+\d+) (.*)', output)
  for rank, device in results:
    metadata[rank] = device
  results = regex_util.ExtractAllMatches(
      r'^\s*'
      r'(\d+)\s+'
      r'(\d+)\s+'
      r'(\w+)\s+'
      r'(\w+)\s+'
      r'(\d+(?:\.\d+)?)\s+'
      r'(\d+(?:\.\d+)?)\s+'
      r'(\d+(?:\.\d+)?)\s+'
      r'(\S+)\s+'
      r'(\d+(?:\.\d+)?)\s+'
      r'(\d+(?:\.\d+)?)\s+'
      r'(\d+(?:\.\d+)?)\s+'
      r'(\S+)', output, re.MULTILINE)
  max_out_of_place_algbw = 0
  for row in results:
    metadata_copy = metadata.copy()
    metadata_copy.update(zip(_METADATA_COLUMNS, row))
    for metric, metadata_key in sorted(_SAMPLE_NAMES.items()):
      samples.append(sample.Sample(metric, float(metadata_copy[metadata_key]),
                                   'GB/s', metadata_copy))
    # Gbps is gigaBIT per second and GB/s is gigaBYTE per second
    max_out_of_place_algbw = max(max_out_of_place_algbw,
                                 float(metadata_copy['out_of_place_algbw']))

  avg_bus_bandwidth = regex_util.ExtractExactlyOneMatch(
      r'Avg bus bandwidth\s+: ([0-9\.]+)', output)
  samples.append(sample.Sample('avg_busbw', float(avg_bus_bandwidth),
                               'GB/s', metadata))
  samples.append(sample.Sample('max_out_of_place_algbw',
                               max_out_of_place_algbw * 8, 'Gbps', metadata))
  return samples, max_out_of_place_algbw


def _TuningPatameters(params):
  """Get all NCCL tuning parameters combination.

  For example:
  params = [
      ('NCCL_NSOCKS_PERTHREAD', ['DEFAULT', '2']),
      ('NCCL_SOCKET_NTHREADS', ['DEFAULT', '8']),
  ]

  result = [
      [],
      [('NCCL_NSOCKS_PERTHREAD', '2')],
      [('NCCL_SOCKET_NTHREADS', '8')],
      [('NCCL_NSOCKS_PERTHREAD', '2'), ('NCCL_SOCKET_NTHREADS', '8')],
  ]

  Args:
    params: list of (parameter name and a list of parameter value)

  Returns:
    a list of all NCCL tuning patameters combination.
  """
  if not params:
    return [[]]
  param_key, param_value_list = params.pop()
  result = []
  for param in _TuningPatameters(params):
    for param_value in param_value_list:
      param_args = [] if param_value == _DEFAULT else [(param_key, param_value)]
      result.append(param + param_args)
  return result


def Run(benchmark_spec):
  """Run NCCL on the cluster.

  Args:
    benchmark_spec: The benchmark specification. Contains all data that is
      required to run the benchmark.

  Returns:
    A list of sample.Sample objects.
  """
  master = benchmark_spec.vms[0]
  env = []
  if FLAGS.nccl_cuda_visible_devices:
    env.append(('CUDA_VISIBLE_DEVICES', FLAGS.nccl_cuda_visible_devices))
  extra_params = collections.defaultdict(list)
  metadata = _CreateMetadataDict()
  sample_results = []
  for extra_param in FLAGS.nccl_extra_params:
    param_key, param_value = extra_param.split('=', 1)
    extra_params[param_key].append(param_value)

  for extra_param in _TuningPatameters(list(extra_params.items())):
    metadata_copy = metadata.copy()
    for param_key, param_value in extra_param:
      metadata_copy[param_key] = param_value
    cmd = _RUN_CMD.format(mpi=FLAGS.nccl_mpi,
                          hostfile=_HOSTFILE,
                          slots=FLAGS.nccl_slots,
                          env=' '.join(
                              '-x {key}={value}'.format(key=key, value=value)
                              for key, value in env + extra_param),
                          minbytes=FLAGS.nccl_minbytes,
                          maxbytes=FLAGS.nccl_maxbytes,
                          stepfactor=FLAGS.nccl_stepfactor,
                          ngpus=FLAGS.nccl_ngpus,
                          check=int(FLAGS.nccl_check),
                          nthreads=FLAGS.nccl_nthreads,
                          iters=FLAGS.nccl_iters)
    max_out_of_place_algbw_results = []

    for iteration in range(FLAGS.nccl_num_runs):
      metadata_copy['run_iteration'] = iteration
      stdout, _ = master.RobustRemoteCommand(cmd)
      samples, max_out_of_place_algbw = MakeSamplesFromOutput(metadata_copy,
                                                              stdout)
      sample_results.extend(samples)
      max_out_of_place_algbw_results.append(max_out_of_place_algbw)
      time.sleep(FLAGS.nccl_seconds_between_runs)
    metadata_copy.pop('run_iteration')
    avg_busbw = [s.value for s in sample_results if s.metric == 'avg_busbw']
    sample_results.append(
        sample.Sample('avg_busbw_mean', np.mean(avg_busbw), 'GB/s',
                      metadata_copy))
    sample_results.append(
        sample.Sample('avg_busbw_std', np.std(avg_busbw), 'GB/s',
                      metadata_copy))
    sample_results.append(
        sample.Sample('max_out_of_place_algbw_mean',
                      np.mean(max_out_of_place_algbw_results), 'Gbps',
                      metadata_copy))
    sample_results.append(
        sample.Sample('max_out_of_place_algbw_std',
                      np.std(max_out_of_place_algbw_results), 'Gbps',
                      metadata_copy))
  return sample_results


def Cleanup(unused_benchmark_spec):
  """Cleanup NCCL on the cluster.

  Args:
    unused_benchmark_spec: The benchmark specification. Contains all data that
      is required to run the benchmark.
  """
  pass
