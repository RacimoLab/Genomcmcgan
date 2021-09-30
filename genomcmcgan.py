import os
import pickle
import time
import argparse
import torch
import torch.nn as nn
import numpy as np
from mcmcgan import MCMCGAN
from discriminator import Discriminator
from genobuilder import Genobuilder
from training_utils import mcmc_diagnostic_plots, plot_disc_acc, plot_pair_evolution


def run_genomcmcgan(
    *,
    genobuilder,
    kernel_name,
    data_path,
    discriminator_model,
    epochs,
    num_mcmc_samples,
    num_mcmc_burnin,
    seed,
    parallelism,
    num_reps_Dx,
    target_acc_rate,
    thinning,
    max_num_iters,
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
    print("Using", torch.cuda.device_count(), "GPUs")
    mcmcgan.discriminator = nn.DataParallel(mcmcgan.discriminator)
    mcmcgan.discriminator.to(device)

    print("Initializing weights of the model")
    mcmcgan.discriminator.apply(mcmcgan.discriminator.module.weights_init)

    print(f"Demographic model for inference - {mcmcgan.genob.demo_model}")
    for p in mcmcgan.genob.params.values():
        print(f"{p.name} inferable: {p.inferable}")

    start_t = time.time()
    means = [0.0]
    accs = []
    batch_size = 32

    while max_num_iters != mcmcgan.iter:

        mcmcgan.iter += 1
        print(f"Starting the MCMC sampling chain for iteration {mcmcgan.iter}")
        t = time.time()

        # Prepare tensors and data loaders with the data and labels
        xtrain = torch.Tensor(xtrain).float()
        ytrain = torch.Tensor(ytrain).float().unsqueeze(-1)
        trainset = torch.utils.data.TensorDataset(xtrain, ytrain)
        trainflow = torch.utils.data.DataLoader(trainset, batch_size=batch_size, shuffle=True)
        xval = torch.Tensor(xval).float()
        yval = torch.Tensor(yval).float().unsqueeze(-1)
        valset = torch.utils.data.TensorDataset(xval, yval)
        valflow = torch.utils.data.DataLoader(valset, batch_size=batch_size, shuffle=True)

        # After wrapping the cnn model with DataParallel, -.module.- is necessary
        accs.append(
            mcmcgan.discriminator.module.fit(
                trainflow=trainflow,
                valflow=valflow,
                epochs=epochs,
                lr=0.0001,
                device=device,
            )
        )

        if len(accs) > 1:
            plot_disc_acc(accs, mcmcgan.iter)

        # Check for convergence
        if accs[-1] < 0.55:
            print("convergence")
            break

        # Run the MCMC sampling step
        mcmcgan.setup_mcmc(
            num_mcmc_results=num_mcmc_samples,
            num_burnin_steps=num_mcmc_burnin,
            thinning=thinning,
            num_reps_Dx=num_reps_Dx,
            target_acc_rate=target_acc_rate,
        )
        mcmcgan.run_chain()

        # Obtain posterior samples and stats for plotting and diagnostics
        posterior, sample_stats = mcmcgan.result_to_stats()
        mcmc_diagnostic_plots(posterior, sample_stats, it=mcmcgan.iter)

        # Draw traceplot and histogram of collected samples
        mcmcgan.traceplot_samples()
        mcmcgan.hist_samples()
        if len(mcmcgan.samples) > 1:
            mcmcgan.jointplot_samples()

        # Calculate means and standard deviations for the next MCMC sampling step
        #means = np.mean(mcmcgan.samples, axis=1)
        medians = np.median(mcmcgan.samples, axis=1)
        stds = np.std(mcmcgan.samples, axis=1)
        for i, p in enumerate(mcmcgan.genob.inferable_params):
            # Update the MCMC stats for each parameter
            print(f"{p.name} samples with median {medians[i]} and std {stds[i]}")
            p.step_size = sample_stats["step_size"][i][-1].numpy()
            p.proposals = mcmcgan.samples[i]
            p.init = mcmcgan.samples[i][-1]

        # Generate new batches of real data and updated simulated data
        xtrain, xval, ytrain, yval = mcmcgan.genob.generate_data(
            num_mcmc_samples, proposals=True
        )

        print(f"A single iteration of the MCMC-GAN took {time.time()-t} seconds")
        print(f"In total, it has been running for {time.time()-start_t} seconds")

    print(f"The estimates obtained after {mcmcgan.iter} iterations are:")
    print(medians)
    plot_pair_evolution(mcmcgan.genob.inferable_params, mcmcgan.kernel_name)


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
        choices=["hmc", "nuts", "randomwalk"],
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

    parser.add_argument(
        "-r",
        "--num-reps-Dx",
        help="Number of simulation replicates to assess discriminator inside the loss function.",
        default=50,
        type=int,
    )

    parser.add_argument(
        "-a",
        "--target-acc-rate",
        help="Target acceptance rate.",
        default=0.5,
        type=float,
    )

    parser.add_argument(
        "-t",
        "--thinning",
        help="Number of MCMC steps between MCMC samples.",
        default=0,
        type=int,
    )

    parser.add_argument(
        "-i",
        "--max-num-iters",
        help="Maximum number of generator-discriminator iterations.",
        default=20,
        type=int,
    )

    # Get argument values from parser
    args = parser.parse_args()

    run_genomcmcgan(
        genobuilder=args.genobuilder,
        kernel_name=args.kernel_name,
        data_path=args.data_path,
        discriminator_model=args.discriminator_model,
        epochs=args.epochs,
        num_mcmc_samples=args.num_mcmc_samples,
        num_mcmc_burnin=args.num_mcmc_burnin,
        seed=args.seed,
        parallelism=args.parallelism,
        num_reps_Dx=args.num_reps_Dx,
        target_acc_rate=args.target_acc_rate,
        thinning=args.thinning,
        max_num_iters=args.max_num_iters,
    )

    # Command example:
    # python genomcmcgan.py geno.pkl -d geno_genmats.pkl -k hmc -e 3 -n 10 -b 5 -se 2020
