<?php

declare(strict_types=1);

namespace Agtp\Symfony\Command;

use Agtp\HandlerRegistry;
use Agtp\ModPhp\GatewayClient;
use Agtp\ModPhp\ModuleException;
use Agtp\Symfony\Registry\AgtpHandlerCollector;
use Symfony\Component\Console\Attribute\AsCommand;
use Symfony\Component\Console\Command\Command;
use Symfony\Component\Console\Input\InputInterface;
use Symfony\Component\Console\Input\InputOption;
use Symfony\Component\Console\Output\OutputInterface;
use Symfony\Component\Console\Style\SymfonyStyle;

/**
 * Symfony Console command: `bin/console agtp:serve`.
 *
 * Symfony has already bootstrapped the kernel by the time the command
 * runs; tagged services are wired into AgtpHandlerCollector via the
 * compiler pass. The command collects them into the agtp-php
 * HandlerRegistry, then runs the gateway client.
 */
#[AsCommand(
    name: 'agtp:serve',
    description: 'Serve AGTP traffic via the local gateway socket.',
)]
final class AgtpServeCommand extends Command
{
    public function __construct(
        private readonly AgtpHandlerCollector $collector,
    ) {
        parent::__construct();
    }

    protected function configure(): void
    {
        $this
            ->addOption(
                'gateway-socket',
                null,
                InputOption::VALUE_REQUIRED,
                'Path to the agtpd gateway socket (or host:port for TCP loopback).',
            )
            ->addOption(
                'module-id',
                null,
                InputOption::VALUE_REQUIRED,
                'Identifier reported in the hello frame.',
                'agtp_symfony',
            )
            ->addOption(
                'module-version',
                null,
                InputOption::VALUE_REQUIRED,
                'Version reported in the hello frame.',
                '0.1.0',
            );
    }

    protected function execute(InputInterface $input, OutputInterface $output): int
    {
        $io = new SymfonyStyle($input, $output);
        $socket = (string) $input->getOption('gateway-socket');
        if ($socket === '') {
            $io->error('--gateway-socket is required');
            return Command::FAILURE;
        }

        $registry = HandlerRegistry::default();
        $count = 0;
        foreach ($this->collector->collect($registry) as $_) {
            $count++;
        }
        $io->note(sprintf('Collected %d AGTP endpoint binding(s).', $count));
        if ($count === 0) {
            $io->warning(
                'No services tagged "agtp.endpoint" were found. Did you ' .
                'forget to tag your handler service?'
            );
        }

        $client = new GatewayClient(
            socketPath: $socket,
            registry: $registry,
            moduleId: (string) $input->getOption('module-id'),
            moduleVersion: (string) $input->getOption('module-version'),
        );

        if (function_exists('pcntl_signal')) {
            pcntl_async_signals(true);
            $shutdown = function () use ($client, $io) {
                $io->writeln('<info>Shutting down on signal.</info>');
                $client->stop();
            };
            pcntl_signal(SIGTERM, $shutdown);
            pcntl_signal(SIGINT, $shutdown);
        }

        try {
            $client->run();
        } catch (ModuleException $exc) {
            $io->error($exc->getMessage());
            return Command::FAILURE;
        }

        return Command::SUCCESS;
    }
}
