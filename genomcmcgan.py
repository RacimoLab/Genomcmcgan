import os
import pickle
import time
import argparse
import torch
import torch.nn as nn
import arviz as az
import numpy as np
from mcmcgan import MCMCGAN
from discriminator import Discriminator
from genobuilder import Genobuilder


def run_genomcmcgan(
    genobuilder,
    kernel_name,
    data_path,
    discriminator_model,
    epochs,
    num_mcmc_samples,
    num_mcmc_burnin,
    seed,
    parallelism,
):

    np.random.seed(seed)

    # Check if folder with results exists, and create it otherwise
    if not os.path.exists("./results"):
        os.makedirs("./results")

    with open(genobuilder, "rb") as obj:
        genob = pickle.load(obj)
    genob.parallelism = parallelism

    # Generate the training and validation datasets
    if data_path:
        with open(data_path, "rb") as obj:
            xtrain, ytrain, xval, yval = pickle.load(obj)
    else:
        xtrain, xval, ytrain, yval = genob.generate_data(num_reps=1000)

    # Initialize the MCMCGAN object and the Discriminator
    mcmcgan = MCMCGAN(genob, kernel_name, seed)
    mcmcgan.discriminator = Discriminator()

    # Use GPUs for Discriminator operations if possible
    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    if torch.cuda.device_count() > 1:
        print("Using", torch.cuda.device_count(), "GPUs")
        # dim = 0 [30, xxx] -> [10, ...], [10, ...], [10, ...] on 3 GPUs
        mcmcgan.discriminator = nn.DataParallel(mcmcgan.discriminator)
    mcmcgan.discriminator.to(device)

    # Prepare tensors and data loaders with the data and labels
    xtrain = torch.Tensor(xtrain).float().to(device)
    ytrain = torch.Tensor(ytrain).float().unsqueeze(-1).to(device)
    trainset = torch.utils.data.TensorDataset(xtrain, ytrain)
    trainflow = torch.utils.data.DataLoader(trainset, 32, True)
    xval = torch.Tensor(xval).float().to(device)
    yval = torch.Tensor(yval).float().unsqueeze(-1).to(device)
    valset = torch.utils.data.TensorDataset(xval, yval)
    valflow = torch.utils.data.DataLoader(valset, 32, True)

    # After wrapping the cnn model with DataParallel, -.module.- is necessary
    mcmcgan.discriminator.module.fit(trainflow, valflow, epochs, lr=0.0001)

    # Create a list with the parameters to infer
    inferable_params = []
    for p in mcmcgan.genob.params.values():
        print(f"{p.name} inferable: {p.inferable}")
        if p.inferable:
            inferable_params.append(p)

    mcmcgan.setup_mcmc(num_mcmc_samples, num_mcmc_burnin, 1)

    max_num_iters = 5
    convergence = False

    while not convergence and max_num_iters != mcmcgan.iter:

        mcmcgan.iter += 1
        print(f"Starting the MCMC sampling chain for iteration {mcmcgan.iter}")
        start_t = time.time()

        # Run the MCMC sampling step
        mcmcgan.run_chain()

        # Obtain posterior samples and stats for plotting and diagnostics
        posterior, sample_stats = mcmcgan.result_to_stats(inferable_params)
        az_trace = az.from_dict(posterior=posterior, sample_stats=sample_stats)
        az.plot_trace(az_trace, compact=True, divergences=False)

        # Draw traceplot and histogram of collected samples
        mcmcgan.traceplot_samples(inferable_params)
        mcmcgan.hist_samples(inferable_params)
        if mcmcgan.samples.shape[1] > 1:
            mcmcgan.jointplot_samples(inferable_params)

        # Update the accepted proposal values for each parameter to infer
        for i, p in enumerate(inferable_params):
            p.proposals = mcmcgan.samples[:, i]

        # Calculate means and standard deviation for next MCMC sampling step
        means = np.mean(mcmcgan.samples, axis=0)
        stds = np.std(mcmcgan.samples, axis=0)
        percentiles = np.percentile(mcmcgan.samples, [2.5, 97.5], axis=0)
        for j, p in enumerate(inferable_params):
            print(f"{p.name} samples with mean {means[j]} and std {stds[j]}")
            p.bounds = tuple(percentiles[:, j])

        inits = means
        step_sizes = stds
        mcmcgan.setup_mcmc(num_mcmc_samples, num_mcmc_burnin, inits, step_sizes, 1)

        # Generate new batches of real data and updated simulated data
        xtrain, xval, ytrain, yval = mcmcgan.genob.generate_data(
            num_mcmc_samples, proposals=True
        )
        xtrain = torch.Tensor(xtrain).float().to(device)
        ytrain = torch.Tensor(ytrain).float().unsqueeze(-1).to(device)
        trainset = torch.utils.data.TensorDataset(xtrain, ytrain)
        trainflow = torch.utils.data.DataLoader(trainset, 32, True)
        xval = torch.Tensor(xval).float().to(device)
        yval = torch.Tensor(yval).float().unsqueeze(-1).to(device)
        valset = torch.utils.data.TensorDataset(xval, yval)
        valflow = torch.utils.data.DataLoader(valset, 32, True)

        # Train the discriminator on the new data
        mcmcgan.discriminator.module.fit(trainflow, valflow, epochs, lr=0.0002)

        t = time.time() - start_t
        print(f"A single iteration of the MCMC-GAN took {t} seconds")

    print(f"The estimates obtained after {mcmcgan.iter} iterations are:")
    print(means)


if __name__ == "__main__":

    # Parser object to collect user input from terminal
    parser = argparse.ArgumentParser(
        description="Markov Chain Monte Carlo-coupled GAN that works with"
        "genotype matrices lodaded with the Genobuilder() class"
    )

    parser.add_argument(
        "genobuilder",
        help="Genobuilder object to use for genotype matrix generation",
        type=str,
    )

    parser.add_argument(
        "-k",
        "--kernel-name",
        help="Type of MCMC kernel to run. See choices for options. Default set to hmc",
        type=str,
        choices=["hmc", "nuts", "random walk"],
        default="hmc",
    )

    parser.add_argument(
        "-d",
        "--data-path",
        help="Path to genotype matrices data stored as a pickle object",
        type=str,
    )

    parser.add_argument(
        "-m",
        "--discriminator-model",
        help="Path to a cnn model to load as the discriminator of the MCMC-GAN as an .hdf5 file",
        type=str,
    )

    parser.add_argument(
        "-e",
        "--epochs",
        help="Number of epochs to train the discriminator on real and fake data on each iteration of MCMCGAN",
        type=int,
        default=5,
    )

    parser.add_argument(
        "-n",
        "--num-mcmc-samples",
        help="Number of MCMC samples to collect in each training iteration of MCMCGAN",
        type=int,
        default=10,
    )

    parser.add_argument(
        "-b",
        "--num-mcmc-burnin",
        help="Number of MCMC burn-in steps in each training iteration of MCMCGAN",
        type=int,
        default=10,
    )

    parser.add_argument(
        "-se",
        "--seed",
        help="Seed for stochastic parts of the algorithm for reproducibility",
        default=None,
        type=int,
    )

    parser.add_argument(
        "-p",
        "--parallelism",
        help="Number of cores to use for simulation. If set to zero, os.cpu_count() is used.",
        default=0,
        type=int,
    )

    # Get argument values from parser
    args = parser.parse_args()

    run_genomcmcgan(
        args.genobuilder,
        args.kernel_name,
        args.data_path,
        args.discriminator_model,
        args.epochs,
        args.num_mcmc_samples,
        args.num_mcmc_burnin,
        args.seed,
        args.parallelism,
    )

    # Command example:
    # python genomcmcgan.py geno.pkl -d geno_genmats.pkl -k hmc -e 3 -n 10 -b 5 -se 2020
