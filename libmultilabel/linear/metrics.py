from __future__ import annotations

import logging
import re
from typing import Literal

import numpy as np

__all__ = ["get_metrics", "compute_metrics", "tabulate_metrics", "MetricCollection"]

logger = logging.getLogger("__main__")
logger.setLevel("INFO")


def _DCG(preds: np.ndarray, target: np.ndarray, k: int = 5) -> np.ndarray:
    """Compute the discounted cumulative gains (DCG)."""
    # Self-implemented dcg is used here. scikit-learn's implementation has
    # an average time complexity O(NlogN) as it directly applies quicksort
    # to preds regardless of k. Here the average time complexity is reduced to
    # O(N + klogk) by first partitioning off the k largest elements and then
    # applying quicksort to the subarray.
    row_idx = np.arange(preds.shape[0])[:, np.newaxis]
    preds_unsorted_idx = np.argpartition(preds, -k)[:, -k:]
    preds_sorted_idx = preds_unsorted_idx[row_idx, np.argsort(-preds[row_idx, preds_unsorted_idx])]

    # target sorted by the top k preds in non-increasing order
    gains = target[row_idx, preds_sorted_idx]

    # the discount factor
    discount = 1 / (np.log2(np.arange(gains.shape[1]) + 2))

    # get the sum product over the last axis of gains and discount
    dcg = gains.dot(discount)
    return dcg


class NDCG:
    def __init__(self, top_k: int):
        """Compute the normalized DCG@k (nDCG@k).

        Args:
            top_k: Consider only the top k elements for each query.
        """
        _check_top_k(top_k)

        self.top_k = top_k
        self.score = 0
        self.num_sample = 0

    def update(self, preds: np.ndarray, target: np.ndarray):
        assert preds.shape == target.shape  # (batch_size, num_classes)

        # DCG
        dcgs = _DCG(preds, target, self.top_k)
        # ideal DCG
        idcgs = _DCG(target, target, self.top_k)
        # normalized DCG
        ndcg_score = dcgs / idcgs

        # deal with instances with all 0 labels and add up the results
        self.score += np.nan_to_num(ndcg_score, nan=0.0).sum()
        # remember the total number of instances
        self.num_sample += preds.shape[0]

    def compute(self) -> float:
        return self.score / self.num_sample

    def reset(self):
        self.score = 0
        self.num_sample = 0


class RPrecision:
    def __init__(self, top_k: int):
        """Compute the R-Precision@K.

        Args:
            top_k: Consider only the top k elements for each query.
        """
        _check_top_k(top_k)

        self.top_k = top_k
        self.score = 0
        self.num_sample = 0

    def update(self, preds: np.ndarray, target: np.ndarray):
        assert preds.shape == target.shape  # (batch_size, num_classes)
        top_k_idx = np.argpartition(preds, -self.top_k)[:, -self.top_k :]
        num_relevant = np.take_along_axis(target, top_k_idx, axis=-1).sum(axis=-1)  # (batch_size, )
        self.score += np.nan_to_num(num_relevant / np.minimum(self.top_k, target.sum(axis=-1)), nan=0.0).sum()
        self.num_sample += preds.shape[0]

    def compute(self) -> float:
        return self.score / self.num_sample

    def reset(self):
        self.score = 0
        self.num_sample = 0


class Precision:
    def __init__(self, num_classes: int, average: str, top_k: int):
        """Compute the Precision@K.

        Args:
            num_classes: The number of classes.
            average: Define the reduction that is applied over labels. Currently only "samples" is supported.
            top_k: Consider only the top k elements for each query.
        """
        if average != "samples":
            raise ValueError("unsupported average")

        _check_top_k(top_k)

        self.top_k = top_k
        self.score = 0
        self.num_sample = 0

    def update(self, preds: np.ndarray, target: np.ndarray):
        assert preds.shape == target.shape  # (batch_size, num_classes)
        top_k_idx = np.argpartition(preds, -self.top_k)[:, -self.top_k :]
        num_relevant = np.take_along_axis(target, top_k_idx, -1).sum()
        self.score += num_relevant / self.top_k
        self.num_sample += preds.shape[0]

    def compute(self) -> float:
        return self.score / self.num_sample

    def reset(self):
        self.score = 0
        self.num_sample = 0


class Recall:
    def __init__(self, num_classes: int, average: str, top_k: int):
        """Compute the Recall@K.

        Args:
            num_classes: The number of classes.
            average: Define the reduction that is applied over labels. Currently only "samples" is supported.
            top_k: Consider only the top k elements for each query.
        """
        if average != "samples":
            raise ValueError("unsupported average")

        _check_top_k(top_k)

        self.top_k = top_k
        self.score = 0
        self.num_sample = 0

    def update(self, preds: np.ndarray, target: np.ndarray):
        assert preds.shape == target.shape  # (batch_size, num_classes)
        top_k_idx = np.argpartition(preds, -self.top_k)[:, -self.top_k :]
        num_relevant = np.take_along_axis(target, top_k_idx, -1).sum(axis=-1)  # (batch_size, )
        # zero label instances treated as 0, to be consistent with torchmetrics
        self.score += np.nan_to_num(num_relevant / target.sum(axis=-1), nan=0.0).sum()
        self.num_sample += preds.shape[0]

    def compute(self) -> float:
        return self.score / self.num_sample

    def reset(self):
        self.score = 0
        self.num_sample = 0


class F1:
    def __init__(self, num_classes: int, average: str, multiclass=False):
        """Compute the F1 score.

        Args:
            num_classes: The number of labels.
            average: Define the reduction that is applied over labels. Should be one of "macro", "micro",
            "another-macro".
            multiclass: Whether the task is a multiclass task.
        """
        self.num_classes = num_classes
        if average not in {"macro", "micro", "another-macro"}:
            raise ValueError("unsupported average")
        self.average = average
        self.multiclass = multiclass
        self.tp = self.fp = self.fn = 0

    def update(self, preds: np.ndarray, target: np.ndarray):
        assert preds.shape == target.shape  # (batch_size, num_classes)
        if self.multiclass:
            max_idx = np.argmax(preds, axis=1).reshape(-1, 1)
            preds = np.zeros(preds.shape)
            np.put_along_axis(preds, max_idx, 1, axis=1)
        else:
            preds = preds > 0
        self.tp += np.logical_and(target == 1, preds == 1).sum(axis=0)
        self.fn += np.logical_and(target == 1, preds == 0).sum(axis=0)
        self.fp += np.logical_and(target == 0, preds == 1).sum(axis=0)

    def compute(self) -> float:
        prev_settings = np.seterr("ignore")

        if self.average == "macro":
            score = np.nansum(2 * self.tp / (2 * self.tp + self.fp + self.fn)) / self.num_classes
        elif self.average == "micro":
            score = np.nan_to_num(2 * np.sum(self.tp) / np.sum(2 * self.tp + self.fp + self.fn))
        elif self.average == "another-macro":
            macro_prec = np.nansum(self.tp / (self.tp + self.fp)) / self.num_classes
            macro_recall = np.nansum(self.tp / (self.tp + self.fn)) / self.num_classes
            score = np.nan_to_num(2 * macro_prec * macro_recall / (macro_prec + macro_recall))

        np.seterr(**prev_settings)
        return score

    def reset(self):
        self.tp = self.fp = self.fn = 0


class MetricCollection(dict):
    """A collection of metrics created by get_metrics.
    MetricCollection computes metric values in two steps. First, batches of
    decision values and labels are added with update(). After all instances have been
    added, compute() computes the metric values from the accumulated batches.
    """

    def __init__(self, metrics):
        self.metrics = metrics

    def update(self, preds: np.ndarray, target: np.ndarray):
        """Adds a batch of decision values and labels.

        Args:
            preds (np.ndarray): A matrix of decision values with dimensions number of instances * number of classes.
            target (np.ndarray): A 0/1 matrix of labels with dimensions number of instances * number of classes.
        """
        assert preds.shape == target.shape  # (batch_size, num_classes)
        for metric in self.metrics.values():
            metric.update(preds, target)

    def compute(self) -> dict[str, float]:
        """Computes the metrics from the accumulated batches of decision values and labels.

        Returns:
            dict[str, float]: A dictionary of metric values.
        """
        ret = {}
        for name, metric in self.metrics.items():
            ret[name] = metric.compute()
        return ret

    def reset(self):
        """Clears the accumulated batches of decision values and labels."""
        for metric in self.metrics.values():
            metric.reset()


def get_metrics(
    monitor_metrics: list[str],
    num_classes: int,
    task: Literal["binary", "multiclass", "multilabel"],
) -> MetricCollection:
    """Get a collection of metrics by their names.
    See MetricCollection for more details.

    Args:
        monitor_metrics (list[str]): A list of metric names.
        num_classes (int): The number of classes.
        task: The type of the classification task.

    Returns:
        MetricCollection: A metric collection of the list of metrics.
    """
    if monitor_metrics is None:
        monitor_metrics = []
    metrics = {}

    for metric in monitor_metrics:
        metric_at_k = re.match(r"(P|R|RP|NDCG)@(\d+)$", metric.upper())

        if metric_at_k is not None:
            metric, k = metric_at_k.groups()
            k = int(k)

            # validate k in metric@k
            if task != "multilabel" and k > 1:
                # make sure k is 1 if the task type is not multilabel
                if f"{metric}@1" in metrics:
                    continue

                logger.warning(
                    f"""The task type is not multilabel and k is expected to be exactly 1. But {metric}@{k} is provided. 
                    A fallback metric {metric}@1 will be used. 
                    Repeated warnings of the same metric will be suppressed."""
                )

                k = 1

            elif task == "multilabel" and k > num_classes:
                # make sure k is less than the number of classes if the task type is multilabel
                if f"{metric}@{num_classes}" in metrics:
                    continue

                logger.warning(
                    f"""k is expected to be less than {num_classes}. But {metric}@{k} is provided. 
                    A fallback metric {metric}@{num_classes} will be used.
                    Repeated warnings of the same metric will be suppressed."""
                )

                k = num_classes

            metrics_key = f"{metric}@{k}"
            if metrics_key in metrics:
                continue

            if metric == "P":
                metrics[metrics_key] = Precision(num_classes, average="samples", top_k=k)
            if metric == "R":
                metrics[metrics_key] = Recall(num_classes, average="samples", top_k=k)
            if metric == "RP":
                metrics[metrics_key] = RPrecision(top_k=k)
            if metric == "NDCG":
                metrics[metrics_key] = NDCG(top_k=k)
        elif metric in {"Another-Macro-F1", "Macro-F1", "Micro-F1"}:
            metrics[metric] = F1(num_classes, average=metric[:-3].lower(), multiclass=task != "multilabel")
        else:
            raise ValueError(f"invalid metric: {metric}")

    return MetricCollection(metrics)


def compute_metrics(
    preds: np.ndarray, target: np.ndarray, monitor_metrics: list[str], multiclass: bool = False
) -> dict[str, float]:
    """Compute metrics with decision values and labels.
    See get_metrics and MetricCollection if decision values and labels are too
    large to hold in memory.


    Args:
        preds (np.ndarray): A matrix of decision values with dimensions number of instances * number of classes.
        target (np.ndarray): A 0/1 matrix of labels with dimensions number of instances * number of classes.
        monitor_metrics (list[str]): A list of metric names.
        multiclass (bool, optional): Enable multiclass mode. Defaults to False.

    Returns:
        dict[str, float]: A dictionary of metric values.
    """
    assert preds.shape == target.shape

    metric = get_metrics(monitor_metrics, preds.shape[1], multiclass)
    metric.update(preds, target)
    return metric.compute()


def tabulate_metrics(metric_dict: dict[str, float], split: str) -> str:
    """Convert a dictionary of metric values into a pretty formatted string for printing.

    Args:
        metric_dict (dict[str, float]): A dictionary of metric values.
        split (str): Name of the data split.

    Returns:
        str: Pretty formatted string.
    """
    msg = f"====== {split} dataset evaluation result =======\n"
    header = "|".join([f"{k:^18}" for k in metric_dict.keys()])
    values = "|".join(
        [f"{x:^18.4f}" if isinstance(x, (np.floating, float)) else f"{x:^18}" for x in metric_dict.values()]
    )
    msg += f"|{header}|\n|{'-----------------:|' * len(metric_dict)}\n|{values}|\n"
    return msg


def _check_top_k(k):
    if not (isinstance(k, int) and k > 0):
        raise ValueError('"k" has to be a positive integer')
