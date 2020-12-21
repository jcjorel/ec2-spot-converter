## Welcome to ec2-spot-converter tool page

This tool converts existing EC2 instances back and forth from on-demand and 'persistent' Spot billing models while preserving
instance attributes (Launch configuration, Tags..), network attributes (existing Private IP addresses, Elastic IP), storage (Volumes),
Elastic Inference accelerators and Elastic GPUs.

It also allows replacement of existing Spot instances with new "identical" ones to update the instance type and cpu options.

