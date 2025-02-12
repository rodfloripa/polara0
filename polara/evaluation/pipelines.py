from __future__ import print_function

from operator import mul as mul_op
from functools import reduce
from random import choice
import pandas as pd
from collections import abc


def is_list_like(obj, allow_sets=False, allow_dict=False):
    return (isinstance(obj, abc.Iterable) and
        not isinstance(obj, (str, bytes)) and
        not (allow_sets is False and isinstance(obj, abc.Set)) and
        not (allow_dict is False and isinstance(obj, abc.Mapping)))


def random_chooser():
    while True:
        values = yield
        yield choice(values)


def random_grid(params, n=60, grid_cache=None, skip_config=None):
    if not isinstance(n, int):
        raise TypeError('n must be an integer, not {}'.format(type(n)))
    if n < 0:
        raise ValueError('n should be >= 0')
    # fix names and order of parameters
    param_names, param_values = zip(*params.items())
    grid = set(grid_cache) if grid_cache is not None else set()
    max_n = reduce(mul_op, [len(vals) for vals in param_values])
    n = min(n if n > 0 else max_n, max_n)

    skipped = set()
    if skip_config is None:
        def never_skip(config): return False
        skip_config = never_skip

    param_chooser = random_chooser()
    try:
        while len(grid) < (n-len(skipped)):
            level_choice = []
            for param_val in param_values:
                next(param_chooser)
                level_choice.append(param_chooser.send(param_val))
            level_choice = tuple(level_choice)
            if skip_config(level_choice):
                skipped.add(level_choice)
                continue
            grid.add(level_choice)
    except KeyboardInterrupt:
        print('Interrupted by user. Providing current results.')
    return grid, param_names


def set_config(model, config, convert_nan=True):
    for name, value in config.items():
        if convert_nan:
            value = value if value == value else None # convert NaN to None
        setattr(model, name, value)


def evaluate_models(models, target_metric='precision', metric_type='all', **kwargs):
    if not is_list_like(models, allow_sets=True):
        models = [models]

    model_scores = {}
    for model in models:
        scores = model.evaluate(metric_type, **kwargs)
        scores = [scores] if not isinstance(scores, list) else scores
        scores_df = pd.concat([pd.DataFrame([s]) for s in scores], axis=1)
        if isinstance(target_metric, str):
            model_scores[model.method] = scores_df[target_metric].squeeze()
        elif callable(target_metric):
            model_scores[model.method] = scores_df.apply(target_metric, axis=1).squeeze()
        else:
            raise NotImplementedError
    return model_scores


def find_optimal_svd_rank(model, ranks, target_metric, return_scores=False,
                          protect_factors=True, config=None, verbose=False,
                          evaluator=None, iterator=lambda x: x, **kwargs):
    evaluator = evaluator or evaluate_models
    model_verbose = model.verbose
    if config:
        set_config(model, config)

    model.rank = svd_rank = max(max(ranks), model.rank)
    if not model._is_ready:
        model.verbose = verbose
        model.build()

    if protect_factors:
        svd_factors = dict(**model.factors) # avoid accidental overwrites

    res = {}
    try:
        for rank in iterator(sorted(ranks, key=lambda x: -x)):
            model.rank = rank
            res[rank] = evaluator(model, target_metric, **kwargs)[model.method]
            # prevent previous scores caching when assigning svd_rank
            model._recommendations = None
    finally:
        if protect_factors:
            model._rank = svd_rank
            model.factors = svd_factors
        model.verbose = model_verbose

    scores = pd.Series(res)
    best_rank = scores.idxmax()
    if return_scores:
        scores.index.name = 'rank'
        scores.name = model.method
        return best_rank, scores.loc[ranks]
    return best_rank


def find_optimal_tucker_ranks(model, tucker_ranks, target_metric, return_scores=False,
                              config=None, verbose=False, same_space=False,
                              evaluator=None, iterator=lambda x: x, **kwargs):
    evaluator = evaluator or evaluate_models
    model_verbose = model.verbose
    if config:
        set_config(model, config)

    model.mlrank = tuple([max(mode_ranks) for mode_ranks in tucker_ranks])

    if not model._is_ready:
        model.verbose = verbose
        model.build()

    factors = dict(**model.factors)
    tucker_rank = model.mlrank

    res_score = {}
    for r1 in iterator(tucker_ranks[0]):
        for r2 in tucker_ranks[1]:
            if same_space and (r2 != r1):
                continue
            for r3 in tucker_ranks[2]:
                if (r1*r2 < r3) or (r1*r3 < r2) or (r2*r3 < r1):
                    continue
                try:
                    model.mlrank = mlrank = (r1, r2, r3)
                    res_score[mlrank] = evaluator(model, target_metric, **kwargs)[model.method]
                    # prevent previous scores caching when assigning tucker_rank
                    model._recommendations = None
                finally:
                    model._mlrank = tucker_rank
                    model.factors = dict(**factors)
    model.verbose = model_verbose

    scores = pd.Series(res_score).sort_index()
    best_mlrank = scores.idxmax()
    if return_scores:
        scores.index.names = ['r1', 'r2', 'r3']
        scores.name = model.method
        return best_mlrank, scores
    return best_mlrank


def params_to_dict(names, params):
    try:
        return dict(zip(names, params))
    except TypeError: # encountered single value
        return {names: params}


def find_optimal_config(model, param_grid, param_names, target_metric, return_scores=False,
                        init_config=None, reset_config=None, verbose=False, force_build=True,
                        evaluator=None, iterator=lambda x: x, **kwargs):
    evaluator = evaluator or evaluate_models
    model_verbose = model.verbose

    if init_config:
        if not is_list_like(init_config):
            init_config = [init_config]
        for config in init_config:
            set_config(model, config)

    model.verbose = verbose
    grid_results = {}
    for params in iterator(param_grid):
        param_config = params_to_dict(param_names, params)
        try:
            set_config(model, param_config)
            if not model._is_ready or force_build:
                model.build()
            grid_results[params] = evaluator(model, target_metric, **kwargs)[model.method]
            
        finally:
            if reset_config is not None:
                if isinstance(reset_config, dict):
                    set_config(model, reset_config)
                elif callable(reset_config):
                    reset_config(model)
                else:
                    raise NotImplementedError

    model.verbose = model_verbose
    # workaround non-orderable configs (otherwise pandas raises error)
    scores = pd.Series(**dict(zip(('index', 'data'),
                                  (zip(*grid_results.items())))))
    best_params = scores.idxmax()
    best_config = params_to_dict(param_names, best_params)

    if return_scores:
        try:
            scores.index.names = param_names
        except ValueError: # not list-like
            scores.index.name = param_names
        scores.name = model.method
        return best_config, scores
    return best_config
