package main

import (
	"flag"
	"fmt"
	"net/netip"
	"os"
	"runtime"

	"github.com/sirupsen/logrus"
	"github.com/slackhq/nebula"
	"github.com/slackhq/nebula/config"
	"github.com/slackhq/nebula/overlay"
	"github.com/slackhq/nebula/util"
	"github.com/vishvananda/netns"
)

// A version string that can be set with
//
//	-ldflags "-X main.Build=SOMEVERSION"
//
// at compile-time.
var Build string

func main() {
	configPath := flag.String("config", "", "Path to either a file or directory to load configuration from")
	configTest := flag.Bool("test", false, "Test the config and print the end result. Non zero exit indicates a faulty config")
	netNs := flag.String("netns", "", "Network namespace to create the TUN device in. If empty, uses current network namespace.")
	printVersion := flag.Bool("version", false, "Print version")
	printUsage := flag.Bool("help", false, "Print command line usage")

	flag.Parse()

	if *printVersion {
		fmt.Printf("Version: %s\n", Build)
		os.Exit(0)
	}

	if *printUsage {
		flag.Usage()
		os.Exit(0)
	}

	if *configPath == "" {
		fmt.Println("-config flag must be set")
		flag.Usage()
		os.Exit(1)
	}

	l := logrus.New()
	l.Out = os.Stdout

	c := config.NewC(l)
	err := c.Load(*configPath)
	if err != nil {
		fmt.Printf("failed to load config: %s", err)
		os.Exit(1)
	}

	var deviceFactory overlay.DeviceFactory
	if *netNs == "" {
		deviceFactory = overlay.NewDeviceFromConfig
	} else {
		deviceFactory = namespacedFactory(*netNs)
	}

	ctrl, err := nebula.Main(c, *configTest, Build, l, deviceFactory)
	if err != nil {
		util.LogWithContextIfNeeded("Failed to start", err, l)
		os.Exit(1)
	}

	if !*configTest {
		// Enter netns here, too, because we need to "see" the interface!
		if *netNs != "" {
			runtime.LockOSThread()
			defer runtime.UnlockOSThread()

			newns, err := netns.GetFromPath(*netNs)
			if err != nil {
				_ = fmt.Errorf("failed to get network namespace: %w", err)
				os.Exit(1)
			}
			defer newns.Close()

			err = netns.Set(newns)
			if err != nil {
				_ = fmt.Errorf("failed to enter network namespace: %w", err)
				os.Exit(1)
			}
		}

		ctrl.Start()
		notifyReady(l)
		ctrl.ShutdownBlock()
	}

	os.Exit(0)
}

func namespacedFactory(path string) overlay.DeviceFactory {
	return func(c *config.C, l *logrus.Logger, tunCidr netip.Prefix, routines int) (overlay.Device, error) {
		runtime.LockOSThread()
		defer runtime.UnlockOSThread()

		origns, err := netns.Get()
		if err != nil {
			return nil, fmt.Errorf("failed to get current network namespace: %w", err)
		}
		defer origns.Close()

		newns, err := netns.GetFromPath(path)
		if err != nil {
			return nil, fmt.Errorf("failed to get given network namespace: %w", err)
		}
		defer newns.Close()

		err = netns.Set(newns)
		if err != nil {
			return nil, fmt.Errorf("failed to enter network namespace: %w", err)
		}

		dev, err := overlay.NewDeviceFromConfig(c, l, tunCidr, routines)
		if err != nil {
			return nil, err
		}

		err = netns.Set(origns)
		if err != nil {
			return nil, fmt.Errorf("failed to leave network namespace: %w", err)
		}

		return dev, nil
	}
}
