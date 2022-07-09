#!/usr/bin/python3
"""
This tool converts EC2 instances back and forth from on-demand and Spot billing models while preserving
network attributes (Private IP addresses, Elastic IP), Storage (Volumes).
It also allows replacement of Spot instances with new ones to update the instance type.

See https://github.com/jcjorel/ec2-spot-converter

Author: jeancharlesjorel@gmail.com
"""
VERSION="::Version::"
RELEASE_DATE="::ReleaseDate::"

import os
import sys
import argparse
import json
import time
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
import pdb

# Configure logging
import logging

LOG_LEVEL = logging.INFO
logger = None
def configure_logging(argv):
    global logger
    if logger is not None:
        return
    logger = logging.getLogger(argv[0])
    logger.setLevel(LOG_LEVEL)
    logger.propagate = False
    ch = logging.StreamHandler()
    ch.setLevel(LOG_LEVEL)
    if LOG_LEVEL == logging.DEBUG:
        logger_formatter = logging.Formatter("[%(levelname)s] %(asctime)s %(filename)s:%(lineno)d - %(message)s")
    else:
        logger_formatter = logging.Formatter("[%(levelname)s] %(asctime)s %(filename)s - %(message)s")
    ch.setFormatter(logger_formatter)
    logger.addHandler(ch)

try:
    import boto3
    from botocore.exceptions import ClientError
    from botocore.exceptions import NoRegionError
    from botocore.config import Config
except:
    logger.exception("")
    raise Exception("Missing critical dependency: 'boto3'. Please install it with 'python3 -m pip install boto3'.")

config = Config(
    retries = {
        'max_attempts': 5,
        'mode': 'standard'
    })
try:
    ec2_client               = boto3.client("ec2",               config=config)
    dynamodb_client          = boto3.client("dynamodb",          config=config)
    elastic_inference_client = boto3.client("elastic-inference", config=config)
    kms_client               = boto3.client("kms",               config=config)
    elbv2_client             = boto3.client("elbv2",             config=config)
    cloudwatch_client        = boto3.client("cloudwatch",        config=config)
except NoRegionError as e:
    print("Please specify an AWS region (either with AWS_DEFAULT_REGION environment variable or using cli profiles - see "
            "https://docs.aws.amazon.com/cli/latest/reference/configure/)!")
    sys.exit(1)

def pprint(json_obj):
    return json.dumps(json_obj, indent=4, sort_keys=True, default=str)

def create_state_table(table_name):
    logger.info(f"Creating DynamoDB table '{table_name}'...")
    response = dynamodb_client.create_table(
        TableName=table_name,
        BillingMode='PAY_PER_REQUEST',
        KeySchema=[
            {
                'AttributeName': 'JobId',
                'KeyType': 'HASH'  # Partition key
            },
        ],
        AttributeDefinitions=[
            {
                'AttributeName': 'JobId',
                'AttributeType': 'S'
            },
            {
                'AttributeName': 'State',
                'AttributeType': 'S'
            },
        ],
        GlobalSecondaryIndexes=[
                {
                    'IndexName': 'State',
                    'KeySchema': [
                        {
                            'AttributeName': 'State',
                            'KeyType': 'HASH'
                        },
                    ],
                    'Projection': {
                        'ProjectionType': 'ALL',
                    }
                },
            ],
    )
    logger.debug(response)

def set_state(attribute, value, force_persist=False):
    global states
    table_name = args["dynamodb_tablename"]
    jobid      = args["instance_id"]
    if attribute is None or str(attribute) == "":
        dynamodb_client.delete_item(
            Key = {"JobId": {"S": jobid}},
            TableName=table_name
            )
        states = {}
    else:
        v = json.dumps(value, default=str)
        if not force_persist and attribute in states and states[attribute] == value:
            return # No need to write again the same value
        expression        = f"set {attribute}=:value"
        expression_values = {
            ':value': { 'S': v}
            }
        response = dynamodb_client.update_item(
            Key = {
                'JobId': {
                    'S': jobid
                }
            },
            TableName=table_name,
            ReturnConsumedCapacity='TOTAL',
            ExpressionAttributeValues=expression_values,
            UpdateExpression=expression
            )
        states[attribute] = value
        logger.debug(response)


states = {}
def read_state_table():
    table_name = args["dynamodb_tablename"]
    jobid      = args["instance_id"]
    query      = {
             'JobId': {
                 'S': jobid
             }
    }
    response = dynamodb_client.get_item(
        TableName=table_name,
        ConsistentRead=True,
        ReturnConsumedCapacity='TOTAL',
        Key=query
        )
    logger.debug(response)
    if "Item" not in response:
        return (True, f"Record '{jobid}' doesn't exist yet.", {
            "JobId": jobid
        })
    kv = {}
    for k in response["Item"]:
        if k == "JobId": continue
        kv[k] = json.loads(response["Item"][k]['S'])
    return (True, f"Record '{jobid}' read succesfully.", kv)

def get_instance_details(instance_id=None):
    try:
        instance_id = args["instance_id"] if instance_id is None else instance_id
        Filters   = [{'Name': 'instance-id', 'Values': [instance_id]}]
        response  = ec2_client.describe_instances(Filters=Filters)
        instances = []
        for reservation in response["Reservations"]:
            instances.extend(reservation["Instances"])
        if len(instances) != 1:
            return None
        return instances[0]
    except Exception as e:
        logger.exception(f"Failed to describe instance! {e}")
        return None

def spot_request_need_cancel(spot_request_id, expected_state=[], wait_for_state=False):
    request = None
    try:
        max_attempts = 30
        while max_attempts:
            max_attempts -= 1
            response      = ec2_client.describe_spot_instance_requests(SpotInstanceRequestIds=[spot_request_id])
            request       = response["SpotInstanceRequests"][0]
            request_state = request["State"]
            if request_state in ["cancelled"]:
                return (False, request)
            elif wait_for_state and request_state not in expected_state:
                logger.info(f"Waiting for Spot Request state be one of {expected_state}... (current state={request_state})") 
                time.sleep(10)
                continue
            elif request_state not in expected_state:
                raise Exception(f"Spot request {spot_request_id} is not in the expected state "
                    f"(state is '{request_state}', should be among {expected_state})!")
            break
        if max_attempts == 0:
            raise Exception("Exception while waiting for Spot request going into the expected state!")
    except ClientError as e:
        if e.response['Error']['Code'] == 'InvalidSpotInstanceRequestID.NotFound':
            return (False, request)
        else:
            raise e
    return (True, request)

def discover_instance_state():
    start_time  = int(time.time())
    instance_id = args["instance_id"]
    instance    = get_instance_details()
    if instance is None:
        raise Exception(f"Can't describe instance '{instance_id}'!")
    logger.debug(pprint(instance))

    set_state("ToolVersion", VERSION)

    # Sanity check
    
    # Check that the tool is not launched from the converted instance id
    try:
        tool_instance_id = open("/sys/class/dmi/id/board_asset_tag").read().split("\n")[0]
        if tool_instance_id == instance_id:
            return (False, f"Can't start conversion of instance {instance_id} as it is the one from where "
                    "you are executing the tool. Please use another EC2 instance to execute 'ec2-spot-converter'!", {})
    except:
        # Can't guess local instance id. No sanity check
        pass

    # Check state of DisableApiTermination of the EC2 instance
    response               = ec2_client.describe_instance_attribute(Attribute='disableApiTermination', InstanceId=instance_id)
    logger.debug(response)
    termination_protection = response["DisableApiTermination"]["Value"]
    if termination_protection:
        return (False, f"Can't convert instance {instance_id}! Termination protection activated! "
                "Please go to AWS console and disable termination protection attribute on this instance.", {})

    cpu_options = None
    if "cpu_options" in args and args["cpu_options"] != "ignore":
        try:
            cpu_options = json.loads(args["cpu_options"])
            logger.info("CPU Options specified: CoreCount=%s, ThreadsPerCore=%s" % (cpu_options["CoreCount"], cpu_options["ThreadsPerCore"]))
        except Exception as e:
            return (False, f"Failed to process '--cpu-options'! Must be JSON format or 'ignore' special value: {e}", {})

    billing_model              = args["target_billing_model"]
    instance_is_spot           = "SpotInstanceRequestId" in instance
    spot_request               = {}
    problematic_spot_condition = False
    if instance_is_spot:
        spot_request_id = instance["SpotInstanceRequestId"]
        # Will throw an Exception if something is wrong with the Spot request
        need_cancel, spot_request = spot_request_need_cancel(spot_request_id, ["open", "active", "disabled"])
        if spot_request["Type"] != "persistent":
            return (False, f"Spot instance type is different from 'persistent' one! (current=%s)" % spot_request["Type"])
        # If we can't retrieve the Spot Request or it is already cancelled, we are in bad shape that require special handling.
        problematic_spot_condition = spot_request is None or spot_request["State"] == "cancelled"

    if billing_model == "spot":
        if "max_spot_price" in args and args["max_spot_price"] <= 0.0:
            return (False, f"--max-spot-price set to a value <= 0.0", {})

        if instance_is_spot:
            if ( not args["force"] and
                 not problematic_spot_condition and
                ("target_instance_type" not in args or instance["InstanceType"] == args["target_instance_type"]) and 
                (cpu_options is None or cpu_options == instance["CpuOptions"])):
                return (False, f"Current instance {instance_id} is already a Spot instance. "
                    "Use --target-billing-model 'on-demand' if you want to convert to 'on-demand' billing model or "
                    "--force to convert this Spot instance to a new one.", {}) 

    if billing_model == "on-demand":
        if not instance_is_spot and not args["force"]:
            return (False, f"Current instance {instance_id} is already an On-Demand instance. "
                "Use --target-billing-model 'spot' if you want to convert to 'spot' billing model or "
                "--force to convert this On-Demand instance to a new one.", {}) 

    # Warn the user of a problematic condition.
    if problematic_spot_condition:
        logger.warning(f"/!\ WARNING /!\ Spot Instance {instance_id} is linked to an invalid Spot Request '{spot_request_id}'! "
                "This situation is known to create issues like difficulty to stop the instance. "
                "If you encounter an IncorrectSpotRequestState Exception while attempting to stop the instance, please "
                "consider converting the running instance AS-IS with '--do-not-require-stopped-instance' option. "
                "[In order to avoid data consistency issues on the host filesystems either set all filesystems read-only "
                "directly in the host + unmount all possible volumes, or 'SW halt' the system (See your Operating System manual for details)]")
        if not args["do_not_pause_on_major_warnings"]:
            logger.warning("Pausing 10s... PLEASE READ ABOVE IMPORTANT WARNING!!! DO 'Ctrl-C' NOW IF YOU NEED SOME TIME TO READ!!")
            time.sleep(10)
    if args["do_not_require_stopped_instance"]:
        if args.get("stop_instance") == True:
            logger.warning("/!\ WARNING /!\ --do-not-require-stopped-instance option is set! As --stop-instance is also set, a stop command "
                "is going to be tried. If it fails, the conversion will continue anyway.") 
        else:
            logger.warning("/!\ WARNING /!\ --do-not-require-stopped-instance option is set! As --stop-instance is NOT set, "
                "the conversion will start directly on the running instance.") 
        if not args["do_not_pause_on_major_warnings"]:
            logger.warning("Pausing 10s... PLEASE READ ABOVE IMPORTANT WARNING!!! DO 'Ctrl-C' NOW IF YOU NEED SOME TIME TO READ!!")
            time.sleep(10)

    if "volume_kms_key_id" in args:
        key_id = args["volume_kms_key_id"]
        try:
            response = kms_client.describe_key(KeyId=key_id)
        except Exception as e:
            return (False, f"Cannot retrieve details of the supplied Volume KMS Key Id: {e}", {})
        logger.debug(response)
        logger.info(f"Valid KMS Key Id specified '{key_id}' (%s)" % response["KeyMetadata"]["Arn"])

    # Get Volume details
    volume_ids     = [blk["Ebs"]["VolumeId"] for blk in instance["BlockDeviceMappings"]]
    response       = ec2_client.describe_volumes(VolumeIds=volume_ids)
    volume_details = response["Volumes"]

    # Get target group registrations
    elb_targets    = get_elb_targets(instance_id)
    if elb_targets is None:
        return (False, f"Failed to retrieve ELB target groups!", {})

    # 'stopped' state management.
    instance_state = instance["State"]["Name"]

    if instance_state != "stopped" and args.get("stop_instance") != True and args.get("do_not_require_stopped_instance") != True:
        return (False, f"Instance '{instance_id}' must be in 'stopped' state (current={instance_state}) ! Use --stop-instance if you want to stop it.", {})

    return (True, f"Instance is in state {instance_state}...", {
        "VolumeDetails": volume_details,
        "ELBTargets": elb_targets,
        "InitialInstanceState": instance,
        "SpotRequest": spot_request,
        "CPUOptions": cpu_options,
        "StartTime": start_time,
        "StartDate": str(datetime.now(tz=timezone.utc))
        })

def stop_instance():
    instance       = states["InitialInstanceState"]
    instance_id    = instance["InstanceId"]
    instance_state = instance["State"]["Name"]

    if args.get("stop_instance") != True and args.get("do_not_require_stopped_instance") == True:
        return (True, f"Instance '{instance_id}' won't be stopped as --do-not-require-stopped-instance is set.", {"FailedStop": True})

    failed_stop = False
    if instance_state == "stopped":
        return (True, "Instance already in 'stopped' state. Pre-requisite passed.", {"FailedStop": failed_stop})

    msg         = f"Instance is in state {instance_state}..."
    if instance_state in ["pending", "running"]:
        try:
            msg = f"Stopping '{instance_id}'..."
            ec2_client.stop_instances(InstanceIds=[instance_id])
        except ClientError as e:
            error_code = e.response['Error']['Code']
            if args["do_not_require_stopped_instance"] and error_code == 'IncorrectSpotRequestState':
                msg = str(f"Received an Exception {error_code} while attempting to stop instance. Continue anyway with the running instance as "
                    "--do-not-require-stopped-state option is set.")
                failed_stop = True
            else:
                raise e
    return (True, msg, {"FailedStop": failed_stop})

def wait_stop_instance():
    failed_stop    = states["FailedStop"]
    instance       = states["InitialInstanceState"]
    instance_state = instance["State"]["Name"]
    max_attempts   = 100
    while not failed_stop and instance_state != "stopped":
        instance = get_instance_details()
        if instance is None:
            return (False, "Can't get instance details! (???)", {})
        instance_state = instance["State"]["Name"]
        if instance_state in ["stopped"]:
            break
        if instance_state in ["terminated"]:
            return (False, "Instance got terminated during stop instance (?!)! Instance terminated by something else during conversion!!"
            " Unrecoverable error!", {})
        logger.info(f"Waiting for instance to stop... (current state={instance_state})")
        time.sleep(15)
        max_attempts  -= 1
        if max_attempts < 0:
            return (False, "Timeout while waiting for 'stopped' state!", {})

    return (True, f"Instance in '{instance_state}' state.", {
        "ConversionStartInstanceState": instance
        })

def tag_all_resources():
    instance    = states["ConversionStartInstanceState"]
    instance_id = instance["InstanceId"]

    volume_ids  = [blk["Ebs"]["VolumeId"] for blk in instance["BlockDeviceMappings"]]
    eni_ids     = [eni["NetworkInterfaceId"] for eni in instance["NetworkInterfaces"]]

    resources = [instance_id]
    resources.extend(eni_ids)
    resources.extend(volume_ids)
    response  = ec2_client.create_tags(Resources=resources, Tags=[{'Key': 'ec2-spot-converter:job-id', 'Value': instance_id}]) 
    logger.debug(response)
    return (True, f"Successfully tagged {resources}.", {
        "EniIds" : eni_ids
        })

def detach_volumes():
    instance    = states["ConversionStartInstanceState"]
    root_device = instance["RootDeviceName"]
    instance_id = instance["InstanceId"]

    detached_ids= []
    for blk in instance["BlockDeviceMappings"]:
        vol = blk["Ebs"]["VolumeId"]
        if vol in detached_ids:
            continue
        if root_device == blk["DeviceName"] or bool(blk["Ebs"]["DeleteOnTermination"]):
            # We detach only volumes with DeleteOnTermination=False and never the Root device.
            continue
        # Detach all volumes that do not share the same lifecycle than the instance.
        response         = ec2_client.describe_volumes(VolumeIds=[vol])
        vol_detail       = response["Volumes"][0]
        stilled_attached = next(filter(lambda a: a["InstanceId"] == instance_id, vol_detail["Attachments"]), None) 
        volume_state     = vol_detail["State"]
        multi_attached   = vol_detail["MultiAttachEnabled"]
        if volume_state == "in-use" and stilled_attached is not None:
            logger.info(f"Detaching volume {vol}... (volume state='{volume_state}', multi-attached='{multi_attached}')")
            ec2_client.detach_volume(Device=blk["DeviceName"], InstanceId=instance_id, VolumeId=vol)
        else:
            # Can happen if the tool has been interrupted and is replayed to redo the step.
            if stilled_attached is None:
                logger.info(f"Volume {vol} is no more attached to instance. Do nothing... (volume state='{volume_state}', multi-attached='{multi_attached}')")
            else:
                logger.info(f"Volume {vol} is not in 'in-use' state. Do nothing... (volume state='{volume_state}', multi-attached='{multi_attached}')")
        detached_ids.append(vol)

    return (True, f"Detached volumes {detached_ids}.", {
        "DetachedVolumes": detached_ids,
        "WithoutExtraVolumesInstanceState": get_instance_details()
        })

def _wait_volume_detach(volume_ids, instance_id):
    max_attempts = 300/5
    while max_attempts and len(volume_ids):
        try:
            response = ec2_client.describe_volumes(VolumeIds=volume_ids)
        except:
            break
        logger.debug(response)
        max_attempts -= 1
        not_avail = [vol for vol in response["Volumes"] if vol["State"] != "available"]
        for vol in not_avail.copy():
            if vol["MultiAttachEnabled"]:
                stilled_attached = next(filter(lambda a: a["InstanceId"] == instance_id, vol["Attachments"]), None)
                logger.info(f"Detected multi-attached volume '%s'. Taking care of this special case..." % vol["VolumeId"]) 
                if stilled_attached is None:
                    not_avail.remove(vol)
        if len(not_avail) == 0:
            break
        if max_attempts % 5 == 0:
            logger.info("Waiting for detached volumes to become 'available'...")
        time.sleep(5)
    if max_attempts == 0:
        return (False, f"Not all volume where 'available' before timeout : {volume_ids}.", {})
    time.sleep(1) # Superstition...
    return (True, f"All detached volumes are 'available' : {volume_ids}.", {})

def wait_volume_detach():
    instance    = states["ConversionStartInstanceState"]
    instance_id = instance["InstanceId"]
    volume_ids  = states["DetachedVolumes"]
    return _wait_volume_detach(volume_ids, instance_id)

def create_ami():
    vol_details = states["VolumeDetails"]
    instance    = states["ConversionStartInstanceState"]
    instance_id = instance["InstanceId"]
    image_name  = f"ec2-spot-converter-{instance_id}"
    root_device = instance["RootDeviceName"]

    # Compute the AMI Device mapping
    kept_blks   = []
    for blk in instance["BlockDeviceMappings"]:
        vol = blk["Ebs"]["VolumeId"]
        if root_device == blk["DeviceName"] or bool(blk["Ebs"]["DeleteOnTermination"]):
            # We always keep the Root device and all volumes with DeleteOnTermination=True as part of the AMI created.
            vol_detail  = next(filter(lambda v: v["VolumeId"] == vol, vol_details), None)
            device_name = blk["DeviceName"] 
            b  = {
                "DeviceName": device_name,
                "Ebs": {
                    "DeleteOnTermination": blk["Ebs"]["DeleteOnTermination"],
                    "VolumeSize": vol_detail["Size"],
                    "VolumeType": vol_detail["VolumeType"],
                }
            }
            ebs = b["Ebs"]
            if vol_detail["VolumeType"] not in ["gp2", "st1", "sc1", "standard"]:
                if "Iops"        in vol_detail: ebs["Iops"]        = vol_detail["Iops"]
                if "Throughput " in vol_detail: ebs["Throughput "] = vol_detail["Throughput"]
            kept_blks.append(b)

    try:
        logger.info(f"AMI Block device mapping: {kept_blks}")
        response= ec2_client.create_image(Name=image_name, InstanceId=instance_id, BlockDeviceMappings=kept_blks)
        logger.debug(response)
    except ClientError as e:
        if e.response['Error']['Code'] != 'InvalidAMIName.Duplicate':
            raise e
        # Fall through when the AMI is already under creation. Could happen in case of step replay after tool interuption

    i = 10
    while (i != 0):
        response = ec2_client.describe_images(Filters=[{
            'Name': 'name',
            'Values': [image_name]
            }])
        if (len(response["Images"]) != 0):
            break
        i -= 1
        logger.info("Waiting for AMI creation to start...")
        time.sleep(5);

    if (i == 0):
        return (False, f"Failed to get ImageId (issue at EC2 API side???) Please try again later...", {})

    image_id = response["Images"][0]["ImageId"]
    return (True, f"AMI image {image_name}/{image_id} started.", {
        "VolumesInAMI": kept_blks,
        "ImageId": image_id
        })


def prepare_network_interfaces():
    instance    = states["WithoutExtraVolumesInstanceState"]

    for eni in instance["NetworkInterfaces"]:
        eni_id   = eni["NetworkInterfaceId"]
        response = ec2_client.modify_network_interface_attribute(
            Attachment={
                'AttachmentId': eni["Attachment"]["AttachmentId"],
                'DeleteOnTermination': False
            },
            NetworkInterfaceId=eni_id)
        logger.debug(response)
    return (True, f"Successfully prepared network interfaces %s." % states["EniIds"], {})

def wait_ami():
    ami_id       = states["ImageId"]
    max_attempts = 720 
    image_state  = ""
    while max_attempts and image_state != "available":
        response      = ec2_client.describe_images(ImageIds=[ami_id])
        logger.debug(response)
        image_state   = response["Images"][0]["State"]
        if image_state == "failed":
            logger.error(f"AMI {ami_id} creation failed! Error returned by EC2 AMI service.")
            response = ec2_client.deregister_image(ImageId=ami_id)
            logger.debug(response)
            rewind_step = get_previous_step_of_step("create_ami")
            set_state("ConversionStep", rewind_step)
            raise Exception(f"The AMI {ami_id} creation failed! Error returned by EC2 AMI service. It can happen rarely... "
                            f"Tool state machine sets back to '{rewind_step}' step. "
                            "Re-run the tool to try again!")
        if image_state == "available":
            break
        logger.info(f"Waiting for image {ami_id} to be available...")
        time.sleep(20)
        max_attempts -= 1
    if max_attempts == 0:
        return (False, f"AMI {ami_id} creation timeout!", {})
    return (True, f"AMI {ami_id} is ready.", {})

def instance_state_checkpoint():
    instance     = states["WithoutExtraVolumesInstanceState"]
    elastic_gpus = []
    if "ElasticGpuAssociations" in instance:
        gpu_ids = []
        for gpu in instance["ElasticGpuAssociations"]:
            gpu_id = gpu["ElasticGpuId"]
            gpu_ids.append(gpu_id)
        response = ec2_client.describe_elastic_gpus(ElasticGpuIds=gpu_ids)
        elastic_gpus = response["ElasticGpuSet"]
    return (True, "Checkpointed instance state.", {
        "InstanceStateCheckpoint": get_instance_details(),
        "ElasticGPUs": elastic_gpus
        })

def get_elb_targets(instance_id):
    if "check_targetgroups" not in args:
        return []
    paginator         = elbv2_client.get_paginator('describe_target_groups')
    query_parameters  = {
            "PaginationConfig": {
                'MaxItems': 3000, # Maximum number of target groups per account
                'PageSize': 200
            }
        }
    if len(args["check_targetgroups"]) or "*" not in args["check_targetgroups"]:
        query_parameters["TargetGroupArns"] = args["check_targetgroups"]
    response_iterator = paginator.paginate(**query_parameters)
    targetgroups      = []
    try:
        for response in response_iterator:
            logger.debug(response)
            for t in response["TargetGroups"]:
                if t["TargetType"] == "instance":
                    targetgroups.append(t)
    except ClientError as e:
        logger.error(f"Failed to list target groups: {e}.")
        return None

    nb_of_targetgroups = len(targetgroups)
    logger.info(f"{nb_of_targetgroups} target groups of type 'instance' will be inspected for possible instance membership. "
            "Note: A large number of target groups could take a lot of time to processs.")

    targets = []
    count   = 0
    for target_group in targetgroups:
        count += 1
        if count % 20 == 0:
            logger.info(f"Processed {count} target groups...")
        target_group_arn = target_group["TargetGroupArn"]
        # Skipped filter by instance id, because if it exists multiple times (with multiple ports) only one of them will
        # be returned
        health_response = elbv2_client.describe_target_health(TargetGroupArn=target_group_arn)
        logger.debug(health_response)
        for target_response in health_response["TargetHealthDescriptions"]:
            if target_response["Target"]["Id"] != instance_id: continue
            target                   = dict(target_response["Target"])
            target["TargetGroupArn"] = target_group_arn
            del target["Id"]
            targets.append(target)
    matching_tg_registrations = len(targets)
    logger.info(f"Found {matching_tg_registrations} target group registrations to preserve for instance {instance_id}...")
    return targets

def deregister_from_target_groups():
    if "ELBTargets" not in states:
        return (True, f"No target group to deregister from", {})
    targets      = states["ELBTargets"]
    instance     = states["InitialInstanceState"]
    instance_id  = instance["InstanceId"]
    for target in targets:
        target_group_arn = target["TargetGroupArn"]
        target_port      = target["Port"]
        logger.info(f"Deregistering from {target_group_arn}... (port={target_port})")
        # Doesn't throw if not registered at this point
        elbv2_client.deregister_targets(TargetGroupArn=target_group_arn, Targets=[{
            "Id": instance_id,
            "Port": target_port
        }])
    targetgroup_arns = [t["TargetGroupArn"] for t in targets]
    return (True, f"Deregistered instance from target groups {targetgroup_arns}.", {})

def drain_elb_target_groups():
    if "ELBTargets" not in states:
        return (True, f"No TargetGroup to drain from", {})
    targets      = states["ELBTargets"]
    instance     = states["InitialInstanceState"]
    instance_id  = instance["InstanceId"]
    for target in targets:
        target_group_arn = target["TargetGroupArn"]
        target_port  = target["Port"]
        max_attempts = 100
        while True:
            response = elbv2_client.describe_target_health(TargetGroupArn=target_group_arn, Targets=[{
                "Id": instance_id,
                "Port": target_port
            }])
            logger.debug(response)
            found_targets = list(filter(lambda t: t["TargetHealth"]["State"] != "unused", response["TargetHealthDescriptions"]))
            if len(found_targets) == 0: break
            max_attempts -= 1
            if max_attempts < 0:
                return (False, "Timeout while waiting for target draining!", {})
            if max_attempts % 3 == 0:
                logger.info(f"Waiting for instance to be drained from {target_group_arn}... (port={target_port})")
            time.sleep(10)
    return (True, f"Drained instance from target groups.", {})

def register_to_elb_target_groups():
    if "ELBTargets" not in states:
        return (True, f"No TargetGroup to register to.", {})
    targets     = states["ELBTargets"]
    instance_id = states["NewInstanceId"]
    for target in targets:
        target_group_arn = target["TargetGroupArn"]
        target_port      = target["Port"]
        logger.info(f"Registering to {target_group_arn}... (port={target_port})")
        # Doesn't throw if already registered at this point
        elbv2_client.register_targets(TargetGroupArn=target_group_arn, Targets=[{
            "Id": instance_id,
            "Port": target_port
        }])
    return (True, f"Successfully registered instance '{instance_id}' in target groups.", {})

def wait_target_groups():
    if "ELBTargets" not in states or len(states["ELBTargets"]) == 0:
        return (True, f"No target group to wait for instance health status.", {})
    targets     = states["ELBTargets"]
    instance_id = states["NewInstanceId"]
    exit_states = ["unused", "healthy"] # Default
    if len(args["wait_for_tg_states"]):
        exit_states = args["wait_for_tg_states"]

    final_states = {}
    for target in targets:
        target_group_arn = target["TargetGroupArn"]
        target_port      = target["Port"]
        max_attempts     = 100
        while True:
            response = elbv2_client.describe_target_health(TargetGroupArn=target_group_arn, Targets=[{
                "Id": instance_id,
                "Port": target_port
            }])
            logger.debug(response)
            tg_descriptions = response["TargetHealthDescriptions"]
            found_targets   = list(filter(lambda t: t["TargetHealth"]["State"] in exit_states, tg_descriptions))
            current_state   = tg_descriptions[0]["TargetHealth"]["State"] if len(tg_descriptions) else "unknown"
            if len(found_targets) == 1: 
                logger.info(f"Instance '{instance_id}' reached expected state '{current_state}' in target group {target_group_arn}.")
                final_states[f"{target_group_arn}:{target_port}"] = current_state
                break
            max_attempts -= 1
            if max_attempts < 0:
                return (False, f"Timeout while waiting for instance to reach expected states {exit_states}!", {})
            if max_attempts % 3 == 0:
                logger.info(f"Waiting for instance status in {target_group_arn} to reach states {exit_states}... "
                    f"(current state={current_state}, port={target_port})")
            time.sleep(10)
    return (True, f"Instance '{instance_id}' has reached expected states in participating target groups: {final_states}.", {})

def terminate_instance():
    instance    = states["InstanceStateCheckpoint"]
    instance_id = instance["InstanceId"]

    if "SpotInstanceRequestId" in instance:
        spot_request_id = instance["SpotInstanceRequestId"]
        # We require the spot request to be in 'open', 'disabled' or 'active' state.
        need_cancel, request = spot_request_need_cancel(spot_request_id, ["open", "disabled", "active"], wait_for_state=True)
        if need_cancel: 
            logger.info(f"Cancelling Spot request {spot_request_id}...")
            response = ec2_client.cancel_spot_instance_requests(SpotInstanceRequestIds=[spot_request_id])
            logger.debug(response)
            time.sleep(5)

    response = ec2_client.terminate_instances(InstanceIds=[instance_id])
    logger.debug(response)
    return (True, f"Successfully terminated instance {instance_id}.", {})

def wait_resource_release():
    instance    = states["InstanceStateCheckpoint"]

    eni_ids      = [eni["NetworkInterfaceId"] for eni in instance["NetworkInterfaces"]]
    max_attempts = 300/5
    while max_attempts and len(eni_ids):
        response = ec2_client.describe_network_interfaces(NetworkInterfaceIds=eni_ids)
        logger.debug(response)
        max_attempts -= 1
        not_avail = [vol for vol in response["NetworkInterfaces"] if vol["Status"] != "available"]
        if len(not_avail) == 0:
            break
        logger.info("Waiting for detached ENIs to become 'available'...")
        time.sleep(5)
    if max_attempts == 0:
        return (False, f"Not all ENIs where 'available' before timeout : {eni_ids}.", {})

    # Wait for terminated instance status
    max_attempts = 300/5
    while max_attempts:
        max_attempts -= 1
        instance      = get_instance_details()
        if instance["State"]["Name"] == "terminated":
            break
        logger.info("Waiting for instance 'terminated' state...")
        time.sleep(5)
    if max_attempts == 0:
        return (False, f"Instance took too long to go to 'terminated' state (???).", {})

    # Manage special case of root device
    instance    = states["InitialInstanceState"]
    instance_id = instance["InstanceId"]
    root_device = instance["RootDeviceName"]
    for blk in instance["BlockDeviceMappings"]:
        vol = blk["Ebs"]["VolumeId"]
        if root_device == blk["DeviceName"] and not bool(blk["Ebs"]["DeleteOnTermination"]):
            logger.info(f"Root volume {vol} is marked as DeleteOnTermination=false. As the root volume is always part of the created AMI, we forcibly "
                    "delete this volume to avoid a leakage.")
            result, reason, keys = _wait_volume_detach([vol], instance_id)
            if not result:
                return (result, reason, keys)
            logger.info(f"Deleting root volume {vol}...")
            try:
                ec2_client.delete_volume(VolumeId=vol)
            except Exception as e:
                logger.warning(f"Failed to delete root volume {vol}... Ignored... : {e}")
            break

    return (True, f"All resources released : {eni_ids}.", {})

def create_new_instance():
    instance       = states["InstanceStateCheckpoint"]
    vol_details    = states["VolumeDetails"]
    kept_blks      = states["VolumesInAMI"]
    ami_id         = states["ImageId"]
    elastic_gpus   = states["ElasticGPUs"]
    spot_request   = states["SpotRequest"]
    cpu_options    = states["CPUOptions"]
    instance_id    = instance["InstanceId"]
    instance_type  = instance["InstanceType"]

    # Sanity check: In case of step replay, we may have already started a new instance but did not have the time 
    #   to persist the next state machine step. We control here if the network interfaces are not already reconnected
    #   to a new instance giving us a good clue of what is the new instance id.
    eni_ids            = [ eni["NetworkInterfaceId"] for eni in instance["NetworkInterfaces"] ] 
    response           = ec2_client.describe_network_interfaces(NetworkInterfaceIds=eni_ids)
    eni_attached_ids   = []
    eni_instance_ids   = []
    for eni in response["NetworkInterfaces"]:
        eni_id     = eni["NetworkInterfaceId"]
        eni_status = eni["Status"]
        if eni_status == "in-use":
            eni_attached_ids.append(eni_id)
            if not "InstanceId" in eni["Attachment"]:
                return (False, f"ENI {eni_id} attached to something else than an EC2 instance!!! (How is it possible??)", {})
            eni_instance_id = eni["Attachment"]["InstanceId"]
            if eni_instance_id not in eni_instance_ids:
                eni_instance_ids.append(eni_instance_id)
        elif eni_status == "available":
            # Just fine! :-)
            continue
        else:
            return (False, f"ENI {eni_id} is in unexpected state (eni state={eni_status})!", {})
    # Guess that a previous step execution succeeded to launch a new instance
    if len(eni_instance_ids) > 0:
        if len(eni_attached_ids) == len(eni_ids) and len(eni_instance_ids) == 1:
            new_instance_id = eni_instance_ids[0]
            # Beyond the reasonable doubt... A new instance succeeded to attach the interface(s).
            return (True, f"Recovered new instance '{new_instance_id}' from previous execution!", {
                "NewInstanceId": new_instance_id,
                })
        else:
            return (False, f"Unconsistencies detected about ENI attachements! (ENI already attached: '{eni_attached_ids}', "
                    f"ENI instance Ids:  '{eni_instance_ids}'", {})


    # Prepare the run_instances() parameters.
    ifaces       = []
    for eni in instance["NetworkInterfaces"]:
        ifaces.append({
            'DeviceIndex': eni["Attachment"]["DeviceIndex"],
            'NetworkInterfaceId': eni["NetworkInterfaceId"],
        })

    # Add Volume encryption if requested.
    if "volume_kms_key_id" in args:
        key_id   = args["volume_kms_key_id"]
        response = kms_client.describe_key(KeyId=key_id)
        logger.debug(response)
        key_arn  = response["KeyMetadata"]["Arn"]
        for blk in kept_blks:
            device_name = blk["DeviceName"]
            for vol_detail in vol_details:
                attachment = next(filter(lambda a: a["Device"] == device_name, vol_detail["Attachments"]), None)
                if attachment is not None:
                    break
            if vol_detail["Encrypted"]:
                logger.warning(f"Device {device_name} is already encrypted with Kms Key Id '%s'. Keep it as is..." % vol_detail["KmsKeyId"])
            else:
                logger.info(f"Device {device_name} marked for encryption with Key Id '{key_arn}'.")
                blk["Ebs"]["Encrypted"] = True
                blk["Ebs"]["KmsKeyId"]  = key_arn

    launch_specifications = {
            'BlockDeviceMappings': kept_blks,
            'EbsOptimized': instance["EbsOptimized"],
            'ImageId': ami_id,
            'InstanceType': instance["InstanceType"] if not "target_instance_type" in args else args["target_instance_type"],
            'Monitoring': {
                'Enabled': instance["Monitoring"]["State"] in ["enabled", "pending"],
            },
            'CapacityReservationSpecification': instance["CapacityReservationSpecification"],
            'NetworkInterfaces': ifaces,
            'Placement': {
                'AvailabilityZone': instance["Placement"]["AvailabilityZone"],
                'Tenancy': instance["Placement"]["Tenancy"]
            },
            'MaxCount': 1,
            'MinCount': 1
        }

    if "KeyName" in instance:
        launch_specifications["KeyName"] = instance["KeyName"]

    if "MetadataOptions" in instance:
        meta_options = instance["MetadataOptions"]
        launch_specifications["MetadataOptions"] = {}
        launch_specifications["MetadataOptions"]["HttpTokens"]              = meta_options["HttpTokens"]
        launch_specifications["MetadataOptions"]["HttpPutResponseHopLimit"] = meta_options["HttpPutResponseHopLimit"]
        launch_specifications["MetadataOptions"]["HttpEndpoint"]            = meta_options["HttpEndpoint"]

    if "EnclaveOptions" in instance:
        launch_specifications["EnclaveOptions"] = {
                "Enabled": instance["EnclaveOptions"]["Enabled"]
            }

    if "Licenses" in instance:
        launch_specifications["LicenseSpecifications"] = instance["Licenses"]

    if "HibernationOptions" in instance:
        if args["ignore_hibernation_options"]:
            logger.info("--ignore-hibernation-options set. Do not copy 'HibernationOptions' in the converted instance.")
        else:
            launch_specifications['HibernationOptions'] = instance["HibernationOptions"]

    if "InstanceInitiatedShutdownBehavior" in instance:
        launch_specifications['InstanceInitiatedShutdownBehavior'] = instance["InstanceInitiatedShutdownBehavior"]

    if "ElasticGpuAssociations" in instance:
        launch_specifications['ElasticGpuSpecification'] = []
        for gpu in elastic_gpus:
            launch_specifications['ElasticGpuSpecification'].append({
                "Type": gpu["ElasticGpuType"]
                })

    if "ElasticInferenceAcceleratorAssociations" in instance: 
        elastic_inf = [ i["ElasticInferenceAcceleratorArn"] for i in instance["ElasticInferenceAcceleratorAssociations"]][0].split("/")[1]
        response = elastic_inference_client.describe_accelerators(acceleratorIds=[elastic_inf])
        if len(response["acceleratorSet"]) != 1:
            return (False, f"Can't describe Elastic Inference id '{elastic_inf}'... (IAM permissions missing?)", {})
        launch_specifications["ElasticInferenceAccelerators"] = {
                "Type": response["acceleratorSet"]["acceleratorType"],
                "Count": len(instance["ElasticInferenceAcceleratorAssociations"])
            }

    # IamInstanceProfile
    if "IamInstanceProfile" in instance:
        launch_specifications["IamInstanceProfile"] = {
            "Arn": instance["IamInstanceProfile"]["Arn"]
            }

    # UserData
    if not args["ignore_userdata"] and "UserData" in instance:
        launch_specifications["UserData"] = instance["UserData"] 

    # CPU Options
    if "CpuOptions" in instance:
        if "target_instance_type" in args:
            if cpu_options is None and args.get("cpu_options") != 'ignore':
                logger.warning("--target-instance-type specified: Do not inherit 'CPU Options' from original instance and "
                    "use instance defaults instead. "
                    "Specify --cpu-options to define new 'CPU Options' settings.")
        elif args.get("cpu_options") != 'ignore':
            # Preserve CpuOptions only if we do not force the instance type
            instance_family = instance_type.split('.')[0]
            if instance["Architecture"] in ["x86_64"] and instance_family not in ["t2", "m1", "m2", "m3"]:
                launch_specifications["CpuOptions"] = instance["CpuOptions"]
    if cpu_options is not None:
        launch_specifications["CpuOptions"] = cpu_options

    # CPU Credits
    if "CreditSpecification" in instance:
        launch_specifications["CreditSpecification"] = instance["CreditSpecification"]

    # Tags
    if "Tags" in instance:
        tags = []
        # Rename reserved aws: namespace tags
        for t in instance["Tags"]:
            if t["Key"].startswith("aws:"):
                logger.warning("Renaming reserved tag '%s' in '_%s' to enable a successful conversion.")
                t["Key"] = "_%s" % t["Key"]
            tags.append(t)
        launch_specifications["TagSpecifications"] = [{
            "ResourceType": "instance",
            "Tags": tags
            }]

    # Spot model
    if args["target_billing_model"] == "spot":
        launch_specifications["InstanceMarketOptions"] = {
            'MarketType': 'spot',
            'SpotOptions': {
                'SpotInstanceType': 'persistent',
                'InstanceInterruptionBehavior': 'stop',
                },
            }
        if spot_request is not None and "SpotPrice" in spot_request:
            if "target_instance_type" in args:
                if "max_spot_price" not in args: 
                    logger.warning("--target-instance-type specified: Do not inherit Spot Price from original instance "
                            "and use On-Demand price instead. "
                            "Please specify --max-spot-price option to set a precise bid.")
            else:
                launch_specifications["InstanceMarketOptions"]["SpotOptions"]["MaxPrice"] = spot_request["SpotPrice"]
        if "max_spot_price" in args:
            logger.info("Setting maximum Spot price to '%s'." % args["max_spot_price"])
            launch_specifications["InstanceMarketOptions"]["SpotOptions"]["MaxPrice"] = str(args["max_spot_price"])

    set_state("NewInstanceLaunchSpecification", launch_specifications)
    response = ec2_client.run_instances(**launch_specifications)
    logger.debug(response)
    new_instance_id = response["Instances"][0]["InstanceId"]
    return (True, f"Created new instance '{new_instance_id}'.", {
        "NewInstanceId": new_instance_id,
        })

def wait_new_instance():
    instance_id  = states["NewInstanceId"]
    status_code  = ""
    max_attempts = 600
    while max_attempts and status_code != "running":
        instance      = get_instance_details(instance_id=instance_id)
        if instance is not None:
            status_code   = instance["State"]["Name"]
        if status_code == "terminated":
            set_state("ConversionStep", get_previous_step_of_step("create_new_instance"))
            return (False, f"Something bad happened during launch of new instance {instance_id}! Instance is now terminated. "
                    "Watch CloudWatch for further indications.", {})
        max_attempts -= 1
        if max_attempts % 30 == 0:
            logger.info("Waiting for instance to come up...")
        time.sleep(0.5)
    if max_attempts == 0:
        return (False, "The new instance did not come up before timeout!", {})

    instance_id = instance["InstanceId"]
    return (True, f"Instance {instance_id} is in 'running' state.", {
        "NewInstanceDetails": instance
        })

def reattach_volumes():
    old_instance_id = states["InitialInstanceState"]["InstanceId"]
    instance_id     = states["NewInstanceId"]
    orig_instance   = states["ConversionStartInstanceState"]
    volume_ids      = states["DetachedVolumes"]
    volume_details  = states["VolumeDetails"]
    
    current_blks    = get_instance_details(instance_id=instance_id)["BlockDeviceMappings"]
    for detail in volume_details:
        vol         = detail["VolumeId"]
        if vol in volume_ids: # Exclude detached volumes
            continue
        tags        = detail["Tags"] if "Tags" in detail else []
        if len(tags) == 0:
            continue
        attachments = detail["Attachments"]
        devicename  = next(filter(lambda d: d["InstanceId"] == old_instance_id, attachments))["Device"]
        blk         = next(filter(lambda v: v["DeviceName"] == devicename, current_blks))
        new_vol     = blk["Ebs"]["VolumeId"]
        logger.info(f"Restoring tags on volume {new_vol} ({devicename})...") 
        response    = ec2_client.create_tags(Resources=[new_vol], Tags=tags) 
        logger.debug(response)

    attached_ids  = []
    for vol in volume_ids:
        if next(filter(lambda v: v["Ebs"]["VolumeId"] == vol, current_blks), None) is not None:
            continue # Already attached. May happen when forcibly replaying the step.
        if vol in attached_ids:
            continue
        blk         = next(filter(lambda v: v["Ebs"]["VolumeId"] == vol, orig_instance["BlockDeviceMappings"]), None)
        device_name = blk["DeviceName"]
        logger.info(f"Attaching volume {vol} to {instance_id} with device name {device_name}...")
        ec2_client.attach_volume(
            Device=device_name,
            InstanceId=instance_id,
            VolumeId=vol)
        attached_ids.append(vol)
    if len(attached_ids) and not args["reboot_if_needed"]:
        logger.warning("/!\ WARNING /!\ Volumes attached after boot. YOUR NEW INSTANCE MAY NEED A REBOOT!")
    return (True, f"Successfully reattached volumes {attached_ids}...", {
        "ReattachedVolumesInstanceState": get_instance_details(instance_id=instance_id)
        })


def configure_network_interfaces():
    new_instance  = states["ReattachedVolumesInstanceState"]
    orig_instance = states["ConversionStartInstanceState"]
    for eni in new_instance["NetworkInterfaces"]:
        eni_id   = eni["NetworkInterfaceId"]
        orig_eni = next(filter(lambda e: eni["NetworkInterfaceId"] == eni_id, orig_instance["NetworkInterfaces"]), None)
        if bool(orig_eni["Attachment"]["DeleteOnTermination"]): 
            logger.info(f"Setting 'DeleteOnTermination=True' for interface {eni_id}...")
            response = ec2_client.modify_network_interface_attribute(
                Attachment={
                    'AttachmentId': eni["Attachment"]["AttachmentId"],
                    'DeleteOnTermination': True
                },
                NetworkInterfaceId=eni_id)
            logger.debug(response)
    return (True, f"Successfully configured network interfaces %s." % states["EniIds"], {})

def manage_elastic_ip():
    instance          = states["InitialInstanceState"]
    response          = ec2_client.describe_addresses()
    eips              = response["Addresses"]
    reassociated_eips = []
    for eni in instance["NetworkInterfaces"]:
        if "Association" in eni:
            public_ip = eni["Association"]["PublicIp"]
            eip       = next(filter(lambda eip: eip["PublicIp"] == public_ip, eips), None)
            if eip is not None:
                response  = ec2_client.associate_address(
                    AllocationId=eip["AllocationId"],
                    NetworkInterfaceId=eni["NetworkInterfaceId"],
                    )
                reassociated_eips.append(eip["PublicIp"])
                logger.debug(response)
    return (True, f"Reassociated EIPs '{reassociated_eips}'.", {})

def reboot_if_needed():
    instance_id   = states["NewInstanceId"]
    volume_ids    = states["DetachedVolumes"]
    if len(volume_ids) == 0:
        return (True, f"No reason to reboot instance '{instance_id}'... Skipping...", {
            "Rebooted": False
            })

    if not args["reboot_if_needed"]:
        return (True, f"It is recommended to reboot '{instance_id}' but --reboot-if-needed option is not set: Do nothing.", {
            "Rebooted": False
            })

    response = ec2_client.reboot_instances(InstanceIds=[instance_id])
    logger.debug(response)
    return (True, f"Successfully rebooted '{instance_id}'.", {
        "Rebooted": True
        })

def update_cloudwatch_alarms():
    new_instance_id = states["NewInstanceId"]
    instance        = states["InitialInstanceState"]
    instance_id     = instance["InstanceId"]

    all_alarms = len(args["update_cw_alarms"]) == 0 or "*" in args["update_cw_alarms"]

    # Prepare the queries
    queries = []
    if all_alarms:
        queries.append({}) # Query all existing CloudWatch alarms
    else:
        for prefix in args["update_cw_alarms"]:
            queries.append({
                "AlarmNamePrefix": prefix
                })

    # Gather alarms that reference the converted Instance Id
    matching_alarm_names = []
    matching_alarms      = []
    for query in queries:
        paginator = cloudwatch_client.get_paginator('describe_alarms')
        for page in paginator.paginate(**query):
            if "MetricAlarms" in page:
                alarms = page["MetricAlarms"]
                for alarm in alarms:
                    alarm_name = alarm["AlarmName"]
                    if alarm_name in matching_alarm_names:
                        continue
                    dimensions = alarm["Dimensions"]
                    instance_id_ref = next(filter(lambda d: d["Name"] == "InstanceId" and d["Value"] == instance_id, dimensions), None)
                    if instance_id_ref is not None:
                        matching_alarm_names.append(alarm_name)
                        matching_alarms.append(alarm)

    # Update matching alarms
    for alarm in matching_alarms:
        alarm_name = alarm["AlarmName"]
        alarm_arn  = alarm["AlarmArn"]
        dimensions = alarm["Dimensions"]
        logger.info(f"Updating CloudWatch alarm '{alarm_name}' ({alarm_arn})...")
        instance_id_ref = next(filter(lambda d: d["Name"] == "InstanceId", dimensions), None)
        instance_id_ref["Value"] = new_instance_id
        params = alarm.copy()
        for p in alarm:
            if p not in ["AlarmName", "AlarmDescription", "ActionsEnabled", "OKActions", "AlarmActions", "InsufficientDataActions", 
                    "MetricName", "Namespace", "Statistic", "ExtendedStatistic", "Dimensions", "Period", "Unit", "EvaluationPeriods", 
                    "DatapointsToAlarm", "Threshold", "ComparisonOperator", "TreatMissingData", "EvaluateLowSampleCountPercentile", 
                    "Metrics", "Tags", "ThresholdMetricId"]:
                del params[p] # Remove unknown parameter key
        cloudwatch_client.put_metric_alarm(**params)
        time.sleep(0.2) # Not too fast to avoid throttling

    return (True, f"Updated CloudWatch alarms '{matching_alarm_names}'.", {})


def untag_resources():
    instance_id   = states["NewInstanceId"]
    orig_instance = states["ConversionStartInstanceState"]
    eni_ids       = [eni["NetworkInterfaceId"] for eni in orig_instance["NetworkInterfaces"]]
    volume_ids    = states["DetachedVolumes"]
    resources     = [instance_id]
    resources.extend(eni_ids)
    resources.extend(volume_ids)

    response  = ec2_client.delete_tags(Resources=resources, Tags=[{'Key': 'ec2-spot-converter:job-id'}]) 
    logger.debug(response)
    return (True, f"Successfully untagged {resources}.", {
        "FinalInstanceState": get_instance_details(instance_id=instance_id),
        "EndTime": time.time()
        })

def deregister_image():
    ami_id = states["ImageId"]

    response = ec2_client.describe_images(ImageIds=[ami_id])
    logger.debug(response)

    img      = response["Images"][0]
    snap_ids = [blk["Ebs"]["SnapshotId"] for blk in img["BlockDeviceMappings"]]
    response = ec2_client.deregister_image(ImageId=ami_id)
    logger.debug(response)

    time.sleep(0.5)
    for snap_id in snap_ids:
        logger.info(f"Deleting snapshot '{snap_id}'...")
        response = ec2_client.delete_snapshot(SnapshotId=snap_id)
        logger.debug(response)

    return (True, f"Successfully deregistered AMI '{ami_id}'.", {
        })

def get_previous_step_of_step(step):
    """Return the previous step of the one passed as parameter.
    """
    prev_s = None
    for s in steps:
        if s["Name"] == step:
            return prev_s
        prev_s = s["Name"]
    return prev_s

steps = [
    {
        "Name" : "read_state_table",
        "PrettyName" : "ReadConfig",
        "Function": read_state_table,
        "Description": "Read DynamoDB state table..."
    },
    {
        "Name" : "discover_instance_state",
        "PrettyName" : "DiscoverInstanceState",
        "Function": discover_instance_state,
        "Description": "Discover instance state..."
    },
    {
        "Name": "deregister_from_target_groups",
        "IfArgs": "check_targetgroups",
        "PrettyName": "DeregisterFromTargetGroups",
        "Function": deregister_from_target_groups,
        "Description": "Deregister from ELB target groups..."
    },
    {
        "Name": "drain_elb_target_groups",
        "IfArgs": "check_targetgroups",
        "PrettyName": "DrainElbTargetGroups",
        "Function": drain_elb_target_groups,
        "Description": "Wait for drainage of ELB target groups..."
    },
    {
        "Name" : "stop_instance",
        "PrettyName" : "StopInstance",
        "Function": stop_instance,
        "Description": "Stop the instance..."
    },
    {
        "Name" : "wait_stop_instance",
        "PrettyName" : "WaitStopInstance",
        "Function": wait_stop_instance,
        "Description": "Wait for expected instance state..."
    },
    {
        "Name" : "tag_all_resources",
        "PrettyName" : "TagAllResources",
        "Function": tag_all_resources,
        "Description": "Tag all resources (Instance, ENI(s), Volumes) with ec2-spot-converter job Id..."
    },
    {
        "Name" : "detach_volumes",
        "PrettyName" : "DetachVolumes",
        "Function": detach_volumes,
        "Description": "Detach instance volumes with DeleteOnTermination=False..."
    },
    {
        "Name" : "wait_volume_detach",
        "PrettyName" : "WaitVolumeDetach",
        "Function": wait_volume_detach,
        "Description": "Wait for volume detach status..."
    },
    {
        "Name" : "create_ami",
        "PrettyName" : "CreateAmi",
        "Function": create_ami,
        "Description": "Start AMI creation..."
    },
    {
        "Name" : "prepare_network_interfaces",
        "PrettyName" : "PrepareNetworkInterfaces",
        "Function": prepare_network_interfaces,
        "Description": "Prepare network interfaces for instance disconnection..."
    },
    {
        "Name" : "wait_ami",
        "PrettyName" : "WaitAmi",
        "Function": wait_ami,
        "Description": "Wait for AMI to be ready..."
    },
    {
        "Name" : "instance_state_checkpoint",
        "PrettyName" : "InstanceStateCheckpoint",
        "Function": instance_state_checkpoint,
        "Description": "Checkpoint the current exact state of the instance..."
    },
    {
        "Name" : "terminate_instance",
        "PrettyName" : "TerminateInstance",
        "Function": terminate_instance,
        "Description": "Terminate instance..."
    },
    {
        "Name" : "wait_resource_release",
        "PrettyName" : "WaitResourceRelease",
        "Function": wait_resource_release,
        "Description": "Wait resource release..."
    },
    {
        "Name" : "create_new_instance",
        "PrettyName" : "CreateInstance",
        "Function": create_new_instance,
        "Description": "Create new instance..."
    },
    {
        "Name" : "wait_new_instance",
        "PrettyName" : "WaitNewInstance",
        "Function": wait_new_instance,
        "Description": "Wait new instance to come up..."
    },
    {
        "Name" : "reattach_volumes",
        "PrettyName" : "ReattachVolumes",
        "Function": reattach_volumes,
        "Description": "Reattach volumes..."
    },
    {
        "Name" : "configure_network_interfaces",
        "PrettyName" : "ConfigureNetworkInterfaces",
        "Function": configure_network_interfaces,
        "Description": "Configure network interfaces..."
    },
    {
        "Name" : "manage_elastic_ip",
        "PrettyName" : "ManageElasticIP",
        "Function": manage_elastic_ip,
        "Description": "Manage Elastic IP..."
    },
    {
        "Name": "register_to_elb_target_groups",
        "IfArgs": "check_targetgroups",
        "PrettyName": "RegisterToElbTargetGroups",
        "Function": register_to_elb_target_groups,
        "Description": "Register instance to ELB target groups.."
    },
    {
        "Name" : "reboot_if_needed",
        "PrettyName" : "RebootIfNeeded",
        "Function": reboot_if_needed,
        "Description": "Reboot new instance (if needed and requested)..."
    },
    {
        "Name" : "update_cloudwatch_alarms",
        "IfArgs": "update_cw_alarms",
        "PrettyName" : "UpdateCloudwathAlarms",
        "Function": update_cloudwatch_alarms,
        "Description": "Update CloudWatch alarms..."
    },
    {
        "Name" : "untag_resources",
        "PrettyName" : "UntagResources",
        "Function": untag_resources,
        "Description": "Untag resources..."
    },
    {
        "Name" : "wait_target_groups",
        "IfArgs": "wait_for_tg_states",
        "PrettyName" : "WaitTargetGroups",
        "Function": wait_target_groups,
        "Description": "Waiting for instance to be at expected states in target groups..."
    },
    {
        "Name" : "deregister_image",
        "IfArgs": "delete_ami",
        "PrettyName" : "DeregisterImage",
        "Function": deregister_image,
        "Description": "Deregister image..."
    },
]

def review_conversion_results():
    o_file = tempfile.NamedTemporaryFile(prefix="original_instance-", suffix=".json", buffering=0)   
    initial_instance_state                        = states["InitialInstanceState"]
    # Ensure the details are sorted the same way
    initial_instance_state["NetworkInterfaces"]   = sorted(initial_instance_state["NetworkInterfaces"], 
            key=lambda i: int(i["Attachment"]["DeviceIndex"]))
    initial_instance_state["Tags"]                = sorted(initial_instance_state.get("Tags", {}), key=lambda t: t["Key"])
    initial_instance_state["BlockDeviceMappings"] = sorted(initial_instance_state["BlockDeviceMappings"], key=lambda t: t["DeviceName"])
    o_file.write(bytes(pprint(initial_instance_state), "utf-8"))

    n_file = tempfile.NamedTemporaryFile(prefix="new_instance-", suffix=".json", buffering=0)   
    final_instance_state                        = states["FinalInstanceState"]
    # Ensure the details are sorted the same way
    final_instance_state["NetworkInterfaces"]   = sorted(final_instance_state["NetworkInterfaces"], 
            key=lambda i: int(i["Attachment"]["DeviceIndex"]))
    final_instance_state["Tags"]                = sorted(final_instance_state.get("Tags", {}), key=lambda t: t["Key"])
    final_instance_state["BlockDeviceMappings"] = sorted(final_instance_state["BlockDeviceMappings"], key=lambda t: t["DeviceName"])
    n_file.write(bytes(pprint(final_instance_state), "utf-8"))
    os.system("vim -c ':syntax off' -d %s %s" % (o_file.name, n_file.name))
    o_file.close()
    n_file.close()


args = {}
default_args = {
        "dynamodb_tablename": "ec2-spot-converter-state-table",
        "target_billing_model": "spot",
        "reboot_if_needed": False,
        "force": False,
        "ignore_userdata": False,
        "ignore_hibernation_options": False,
        "debug": False,
        "do_not_pause_on_major_warnings": False,
        "do_not_require_stopped_instance": False
    }

def main(argv):
    global args
    global LOG_LEVEL
    if "--version" in argv or "-v" in argv:
        print(f"{VERSION} ({RELEASE_DATE})")
        return 0

    require_instance_id = ("--generate-dynamodb-table" not in argv and
                           "--reset-step" not in argv)
    parser = argparse.ArgumentParser(description=f"EC2 Spot converter {VERSION} ({RELEASE_DATE})")
    parser.add_argument('-i', '--instance-id', help="The id of the EC2 instance to convert.", 
            type=str, required=require_instance_id, default=argparse.SUPPRESS)
    parser.add_argument('-m', '--target-billing-model', help="The expected billing model after conversion. "
            "Default: 'spot'", choices=["spot", "on-demand"],
            type=str, required=False, default=argparse.SUPPRESS)
    parser.add_argument('-t', '--target-instance-type', help="The expected instance type (ex: m5.large...) after conversion. "
            "This flag is only useful when applied to a Spot instance as EC2.modify_instance_attribute() can't be used "
            "to change Instance type. "
            "Default: <original_instance_type>", 
            type=str, required=False, default=argparse.SUPPRESS)
    parser.add_argument('--ignore-userdata', help="Do not copy 'UserData' on converted instance.",
            action='store_true', required=False, default=argparse.SUPPRESS)
    parser.add_argument('--ignore-hibernation-options', help="Do not copy 'HibernationOptions' on converted instance.",
            action='store_true', required=False, default=argparse.SUPPRESS)
    parser.add_argument('--cpu-options', help="Instance CPU Options JSON structure. "
            'Format: {"CoreCount":123,"ThreadsPerCore":123}. Note: The special \'ignore\' value will force to not define the CPUOptions '
            'structure in the new EC2 Launch specification.',
            type=str, required=False, default=argparse.SUPPRESS)
    parser.add_argument('--max-spot-price', help="Maximum hourly price for Spot instance target. Default: On-Demand price.", 
            type=float, required=False, default=argparse.SUPPRESS)
    parser.add_argument('--volume-kms-key-id', help="Identifier (key ID, key alias, ID ARN, or alias ARN) for a Customer or AWS managed KMS Key "
            "used to encrypt the EBS volume(s). Note: You cannot specify 'aws/ebs' directly, please specify "
            "the plain KMS Key ARN instead. It applies ONLY to volumes placed in the Backup AMI *AND* not already encrypted.", 
            type=str, required=False, default=argparse.SUPPRESS)
    parser.add_argument('-s', '--stop-instance', help="Stop instance instead of failing because it is in 'running' state.",
            action='store_true', required=False, default=argparse.SUPPRESS)
    parser.add_argument('--reboot-if-needed', help="Reboot the new instance if needed.", 
            action='store_true', required=False, default=argparse.SUPPRESS)
    parser.add_argument('--update-cw-alarms', help="Update CloudWatch alarms with reference to the converted Instance Id. "
            "Optionnaly, a CloudWatch alarm name prefix list can be supplied to narrow instance id lookup to a subset of matching alarm names. "
            "Without args, all CloudWatch alarms in the current account will be searched.",
            nargs='*', required=False, default=argparse.SUPPRESS)
    parser.add_argument('--delete-ami', help="Delete AMI at end of conversion.", 
            action='store_true', required=False, default=argparse.SUPPRESS)
    parser.add_argument('--check-targetgroups', help="List of target group ARNs to look for converted instance registrations. Without parameter specified, it means all "
            "ELB target groups in the current region (WARNING: An account can contain up to 3000 target groups and induce long "
            "processing time). Default: Feature is disabled when option is not on the command line.",
            nargs='*', required=False, default=argparse.SUPPRESS)
    parser.add_argument('--wait-for-tg-states', help="Wait for target group registrations to reach specified state(s) at end of "
            "conversion. Default: ['unused', 'healthy']",
            nargs='*', required=False, choices=["unused", "unhealthy", "healthy", "initial", "draining"])
    parser.add_argument('--do-not-require-stopped-instance', help="Allow instance conversion while instance is in 'running' state. (NOT RECOMMENDED)", 
            action='store_true', required=False, default=argparse.SUPPRESS)
    parser.add_argument('-r', '--review-conversion-result', help="Display side-by-side conversion result. Note: REQUIRES 'VIM' EDITOR!", 
            action='store_true', required=False, default=argparse.SUPPRESS)
    parser.add_argument('--dynamodb-tablename', help="A DynamoDB table name to hold conversion states. "
            "Default: 'ec2-spot-converter-state-table'", 
            type=str, required=False, default=argparse.SUPPRESS)
    parser.add_argument('--generate-dynamodb-table', help="Create a DynamoDB table to hold conversion states.",
            action='store_true', required=False, default=argparse.SUPPRESS)
    parser.add_argument('-f', '--force', help="Force to start a conversion even if the tool suggests that it is not needed.", 
            action='store_true', required=False, default=argparse.SUPPRESS)
    parser.add_argument('--do-not-pause-on-major-warnings', help="Do not pause on major warnings. Without this flag, the tool waits 10 seconds to "
            "let user read major warnings.", 
            action='store_true', required=False, default=argparse.SUPPRESS)
    parser.add_argument('--reset-step', help="(DANGEROUS) Force the state machine to go back to the specified processing step.",
            type=int, required=False, default=argparse.SUPPRESS)
    parser.add_argument('-d', '--debug', help="Turn on debug traces.", 
            action='store_true', required=False, default=argparse.SUPPRESS)
    parser.add_argument('-v', '--version', help="Display tool version.", 
            action='store_true', required=False, default=argparse.SUPPRESS)
    cmdargs = {}
    for a in parser.parse_args()._get_kwargs():
        cmdargs[a[0]] = a[1]
    args = cmdargs.copy()
    for a in default_args:
        if a not in args or args[a] is None: args[a] = default_args[a]

    LOG_LEVEL=logging.DEBUG if args["debug"] else logging.INFO
    configure_logging(argv)

    if "generate_dynamodb_table" in cmdargs:
        create_state_table(args["dynamodb_tablename"])
        return 0

    step_names = [s["Name"] for s in steps]

    ## Manage tricks to force the state machine 
    if "reset_step" in cmdargs:
        logger.warning("/!\ WARNING /!\ You are manipulating the tool state machine. Make sure you know what you are doing!")
        expected_step = int(cmdargs["reset_step"])
        if expected_step < 1:
            logger.error("Expected step can't be below 1.")
            return 1
        elif expected_step == 1:
            set_state("", "") # Discard the DynamoDB record
            return 0
        elif expected_step <= len(step_names):
            set_state("ConversionStep", step_names[expected_step-1])
            return 0
        else:
            logger.error("Expected state can't be above %s." % len(step_names))
            return 1

    start_time = time.time()
    for i in range(0, len(steps)):
        step      = steps[i]
        step_name = step["Name"]
        if "IfArgs" in step and args.get(step["IfArgs"]) is None:
            logger.info(f"[STEP %d/%d] %s => SKIPPED! Need '--%s' argument." % 
                    (i + 1, len(steps), step["Description"], step["IfArgs"].replace("_","-")))
            continue
        if "IfNotArgs" in step and args.get(step["IfNotArgs"]) is not None:
            logger.info(f"[STEP %d/%d] %s => SKIPPED! Remove '--%s' argument." %
                    (i + 1, len(steps), step["Description"], step["IfNotArgs"].replace("_","-")))
            continue
        display_status = ""
        if "ConversionStep" in states:
            current_step=states["ConversionStep"]
            last_succes_step = step_names.index(current_step)
            if i <= last_succes_step:
                display_status = ": RECOVERED STATE. SKIPPED!"
        logger.info(f"[STEP %d/%d] %s %s" % (i + 1, len(steps), step["Description"], display_status))

        # Warn the user when command line has changed between invocation
        if i > 0 and "ConversionStepCmdLineArgs" in states:
            prev_step_index = i-1
            prev_step       = steps[prev_step_index]
            # If previous step was skipped, compare to the one before it
            while ("IfArgs" in prev_step and args.get(prev_step["IfArgs"]) is None) or ("IfNotArgs" in step and args.get(step["IfNotArgs"]) is not None):
                prev_step_index = prev_step_index - 1
                prev_step       = steps[prev_step_index]
            prev_step_name = prev_step["Name"]
            prev_step_args = states["ConversionStepCmdLineArgs"][prev_step_name]
            current_args   = args if display_status == "" else states["ConversionStepCmdLineArgs"][steps[i]["Name"]]
            if prev_step_args != current_args:
                changed_args = {}
                for arg in current_args:
                    if arg in prev_step_args:
                        if prev_step_args[arg] != current_args[arg]:
                            changed_args[arg] = [prev_step_args[arg], current_args[arg]]
                    else:
                        changed_args[arg] = [None, current_args[arg]]
                logger.warning(f"/!\ WARNING /!\ Tool command line has changed compared to previous step : {{ARG:[OLD, NEW VALUE]}} => {changed_args}!")

        # If step already played, print the former result.
        if display_status != "":
            if "ConversionStepReasons" in states and step_name in states["ConversionStepReasons"]:
                logger.info(f"  => SUCCESS. %s" % states["ConversionStepReasons"][step_name])
            continue

        return_code, reason, keys = step["Function"]()
        if not return_code:
            logger.error(f"Failed to perform step '%s'! Reason={reason}" % step["PrettyName"])
            return 1
        logger.info(f"  => SUCCESS. {reason}")
        set_state("ConversionStep", step_name)

        # Keep track of reasons
        reasons            = states["ConversionStepReasons"] if "ConversionStepReasons" in states else {}
        reasons[step_name] = reason
        set_state("ConversionStepReasons", reasons, force_persist=True)
        # Keep track of user supplied command line parameters
        step_cmdlines            = states["ConversionStepCmdLineArgs"] if "ConversionStepCmdLineArgs" in states else {}
        step_cmdlines[step_name] = args
        set_state("ConversionStepCmdLineArgs", step_cmdlines, force_persist=True)

        # Persist known keys
        for s in keys:
            if s != "JobId": set_state(s, keys[s])

    logger.debug(pprint(states["FinalInstanceState"]))
    logger.info("Conversion successful! New instance id: %s, ElapsedTime: %s seconds" % 
            (states["NewInstanceId"], int(states["EndTime"] - states["StartTime"]))) 

    if "review_conversion_result" in args and args["review_conversion_result"]:
        review_conversion_results()

    if "DetachedVolumes" in states and len(states["DetachedVolumes"]) and not args["reboot_if_needed"]:
        logger.warning("/!\ WARNING /!\ Volumes attached after boot. YOUR NEW INSTANCE MAY NEED A REBOOT!")

    return 0

if __name__ == '__main__':
    try:
        sys.exit(main(sys.argv))
    except KeyboardInterrupt as e:
        print()
        logger.error("TOOL INTERRUPTED BY USER! You can restart the tool with same arguments to continue the conversion "
                "where it has been interrupted.")
        sys.exit(1)
