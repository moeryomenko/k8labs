# Installing the Client Tools

In this lab you will install the command line utilities required to complete this tutorial:
- [cfssl](https://github.com/cloudflare/cfssl)
- [cfssljson](https://github.com/cloudflare/cfssl)
- [kubectl](https://kubernetes.io/docs/tasks/tools/install-kubectl).


## Install CFSSL

The `cfssl` and `cfssljson` command line utilities will be used to provision a [PKI Infrastructure](https://en.wikipedia.org/wiki/Public_key_infrastructure) and generate TLS certificates.

Download and install `cfssl` and `cfssljson`:

```
./ctl.sh install cfssl
```

### Verification

Verify `cfssl` and `cfssljson` version 1.3.4 or higher is installed:

```
$ cfssl version
Version: 1.3.4
Revision: dev
Runtime: go1.13
```

```
$ cfssljson --version
Version: 1.3.4
Revision: dev
Runtime: go1.13
```

## Install kubectl

The `kubectl` command line utility is used to interact with the Kubernetes API Server. Download and install `kubectl` from the official release binaries:

```
./ctl.sh install kubectl
```

### Verification

Verify `kubectl` version 1.23.5 or higher is installed:

```
$ kubectl version --client
Client Version: version.Info{Major:"1", Minor:"23", GitVersion:"v1.23.5", GitCommit:"c285e781331a3785a7f436042c65c5641ce8a9e9", GitTreeState:"clean", BuildDate:"2022-03-16T15:58:47Z", GoVersion:"go1.17.8", Compiler:"gc", Platform:"linux/amd64"}
```


Next: [Provisioning Compute Resources](03-compute-resources.md)
