# Tiny Classifier: A Synthetic Paper for Local Smoke Tests

> This document is a repository fixture. It describes a small synthetic task so the planning workflow can run without publishing a third-party paper.

## Abstract

We study a two-class tabular classification task and describe a compact multilayer perceptron baseline. The goal is to demonstrate the Paper2Repo planning flow with a small, self-contained input.

## Method

The model receives a vector of 16 floating-point features. It uses two linear layers with a ReLU activation between them and produces logits for two classes. Cross-entropy is used as the training objective.

## Data

The smoke dataset contains synthetic feature vectors sampled from a bounded normal distribution. Labels are integers in the range 0 to 1. The workflow may generate a synthetic fallback when no downloadable dataset is specified.

## Training

Train with Adam, learning rate 0.001, batch size 8, and a fixed random seed. A short smoke run is sufficient for this fixture.

## Evaluation

Report validation accuracy and the shape of the model output. The primary engineering check is that the generated repository imports successfully and its forward pass returns two-class logits.
