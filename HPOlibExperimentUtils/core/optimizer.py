import logging
from copy import deepcopy
from pathlib import Path
from time import time

import numpy as np
from hpbandster.core import result as hpres, nameserver as hpns
from hpbandster.optimizers import BOHB
from smac.facade.smac_hpo_facade import SMAC4HPO
from smac.intensification.hyperband import Hyperband
from smac.intensification.successive_halving import SuccessiveHalving
from smac.scenario.scenario import Scenario

from HPOlibExperimentUtils.utils.optimizer_utils import CustomWorker, get_number_ta_runs, parse_fidelity_type
from HPOlibExperimentUtils.utils.runner_utils import OptimizerEnum
from HPOlibExperimentUtils.utils.utils import TimeoutException, time_limit

logger = logging.getLogger('Optimizer')


class Optimizer:
    def __init__(self, benchmark, optimizer_settings, benchmark_settings, intensifier, rng=0):
        self.benchmark = benchmark
        self.cs = benchmark.get_configuration_space()
        self.rng = rng
        self.optimizer_settings = optimizer_settings
        self.benchmark_settings = benchmark_settings
        self.intensifier = intensifier

    def setup(self):
        raise NotImplementedError()

    def run(self) -> Path:
        raise NotImplementedError()


class BOHBOptimizer(Optimizer):
    def __init__(self, benchmark, optimizer_settings, benchmark_settings, intensifier, rng=0):
        super().__init__(benchmark, optimizer_settings, benchmark_settings, intensifier, rng)
        self.run_id = f'BOHB_optimization_seed_{self.rng}'

    def setup(self):
        pass

    def run(self):
        result_logger = hpres.json_result_logger(directory=str(self.optimizer_settings['output_dir']), overwrite=True)

        ns = hpns.NameServer(run_id=self.run_id,
                             host='localhost',
                             working_directory=str(self.optimizer_settings['output_dir']))
        ns_host, ns_port = ns.start()

        worker = CustomWorker(benchmark=self.benchmark,
                              benchmark_settings=self.benchmark_settings,
                              nameserver=ns_host,
                              nameserver_port=ns_port,
                              run_id=self.run_id)

        worker.run(background=True)

        master = BOHB(configspace=self.cs,
                      run_id=self.run_id,
                      host=ns_host,
                      nameserver=ns_host,
                      nameserver_port=ns_port,
                      eta=self.optimizer_settings['eta'],
                      min_budget=self.optimizer_settings['min_budget'],
                      max_budget=self.optimizer_settings['max_budget'],
                      result_logger=result_logger)

        # TODO: Do it the same way as in smac: try with finally
        try:
            with time_limit(self.optimizer_settings['time_limit_in_s']):
                result = master.run(n_iterations=self.optimizer_settings['num_iterations'])
        except TimeoutException:
            for iteration in master.warmstart_iteration:
                iteration.fix_timestamps(master.time_ref)
            ws_data = [iteration.data for iteration in master.warmstart_iteration]
            result = hpres.Result([deepcopy(iteration.data) for iteration in master.iterations]+ws_data, master.config)
            logger.info('WALLCLOCK LIMIT REACHED')

        master.shutdown(shutdown_workers=True)
        ns.shutdown()

        if result is not None:
            id2config = result.get_id2config_mapping()
            incumbent = result.get_incumbent_id()
            inc_value = result.get_runs_by_id(incumbent)[-1]['loss']
            inc_cfg = id2config[incumbent]['config']

            logger.info(f'Inc Config:\n{inc_cfg}\n with Performance: {inc_value:.2f}')

        return self.optimizer_settings['output_dir']


class SMACOptimizer(Optimizer):
    def __init__(self, benchmark, optimizer_settings, benchmark_settings, intensifier, rng=0):
        super().__init__(benchmark, optimizer_settings, benchmark_settings, intensifier, rng)

        if intensifier is OptimizerEnum.HYPERBAND:
            self.intensifier = Hyperband
        elif intensifier is OptimizerEnum.SUCCESSIVE_HALVING:
            self.intensifier = SuccessiveHalving
        else:
            # TODO: Increase the supported intensifier
            raise ValueError('Currently no other intensifier is supported')

    def setup(self):
        pass

    def run(self):
        number_ta_runs = get_number_ta_runs(iterations=self.optimizer_settings['num_iterations'],
                                            min_budget=self.optimizer_settings['min_budget'],
                                            max_budget=self.optimizer_settings['max_budget'],
                                            eta=self.optimizer_settings['eta'])

        scenario_dict = {"run_obj": "quality",
                         "wallclock-limit": self.optimizer_settings['time_limit_in_s'],
                         "cs": self.cs,
                         "deterministic": "true",
                         "limit_resources": True,
                         "runcount-limit": number_ta_runs,  # max. number of function evaluations
                         "cutoff": self.optimizer_settings['cutoff_in_s'],
                         "memory_limit": self.optimizer_settings['mem_limit_in_mb'],
                         "output_dir": str(self.optimizer_settings['output_dir']),
                         }
        scenario = Scenario(scenario_dict)

        def optimization_function_wrapper(cfg, seed, instance, budget):
            """ Helper-function: simple wrapper to use the benchmark with smac"""
            fidelity_type = parse_fidelity_type(self.benchmark_settings['fidelity_type'])
            fidelity = {self.benchmark_settings['fidelity_name']: fidelity_type(budget)}
            result_dict = self.benchmark.objective_function(cfg, **fidelity, **self.benchmark_settings)
            return result_dict['function_value']

        smac = SMAC4HPO(scenario=scenario,
                        rng=np.random.RandomState(self.rng),
                        tae_runner=optimization_function_wrapper,
                        intensifier=self.intensifier,  # you can also change the intensifier to use like this!
                        intensifier_kwargs={'initial_budget': self.optimizer_settings['min_budget'],
                                            'max_budget': self.optimizer_settings['max_budget'],
                                            'eta': self.optimizer_settings['eta']}
                        )
        start_time = time()
        try:
            smac.optimize()
        finally:
            incumbent = smac.solver.incumbent
        end_time = time()
        logger.info(f'Finished Optimization after {int(end_time - start_time):d}s. Incumbent is {incumbent}')

        return Path(smac.output_dir)
